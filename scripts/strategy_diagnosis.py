from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from clients.schemas import Candle
from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from scripts.phase2c_shadow_analysis import performance


SEED = 20260715
BOOTSTRAP_SAMPLES = 5000
STRATEGIES = (
    "momentum_breakout", "momentum_breakdown", "trend_continuation",
    "liquidity_sweep_reversal", "low_vol_reclaim", "adaptive_momentum_continuation",
)


def load_candles(path: Path) -> dict[str, list[Candle]]:
    result = {}
    for file in sorted(path.glob("*.json")):
        payload = json.loads(file.read_text())
        rows = payload.get("candles", payload) if isinstance(payload, dict) else payload
        result[file.stem.upper()] = [Candle(
            int(row.get("timestamp_ms", row.get("timestamp"))), float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]), float(row["volume_base"]),
            float(row["volume_quote"]) if row.get("volume_quote") is not None else None,
        ) for row in rows]
    return result


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _directional(direction: str, entry: float, price: float) -> float:
    return price - entry if direction == "LONG" else entry - price


def excursion(record: dict[str, str], candles: list[Candle], horizon: int = 16) -> dict[str, Any]:
    fill_ts = int(record["fill_timestamp"])
    start = next((i for i, candle in enumerate(candles) if candle.timestamp_ms == fill_ts), None)
    if start is None:
        raise ValueError("fill candle absent from canonical data")
    path = candles[start + 1:start + 1 + horizon]
    entry = float(record["executed_entry"])
    stop = float(record["initial_stop"])
    risk = abs(entry - stop)
    direction = record["direction"]
    favourable = [(c.high - entry if direction == "LONG" else entry - c.low) for c in path]
    adverse = [(entry - c.low if direction == "LONG" else c.high - entry) for c in path]
    mfe = max([0.0] + favourable)
    mae = max([0.0] + adverse)
    time_mfe = favourable.index(mfe) + 1 if mfe > 0 else None
    time_mae = adverse.index(mae) + 1 if mae > 0 else None
    tp1 = float(record["tp1_price"])
    requested = float(record["requested_entry"])
    target2 = requested + 1.5 * abs(requested - stop) * (1 if direction == "LONG" else -1)
    milestones = {name: risk * multiple for name, multiple in (("r025", .25), ("r050", .5), ("r075", .75), ("r100", 1.0))}
    milestones["tp1"] = _directional(direction, entry, tp1)
    milestones["final"] = _directional(direction, entry, target2)
    reached = {name: any(value >= distance for value in favourable) if distance > 0 else False for name, distance in milestones.items()}
    times = {name: next((i + 1 for i, value in enumerate(favourable) if value >= distance), None) if distance > 0 else None for name, distance in milestones.items()}
    stop_time = next((i + 1 for i, value in enumerate(adverse) if value >= risk), None)
    first_favourable = next((i + 1 for i, value in enumerate(favourable) if value > 0), None)
    first_adverse = next((i + 1 for i, value in enumerate(adverse) if value > 0), None)
    if stop_time == 1 and not reached["r025"]:
        quality = "immediate failure"
    elif reached["final"]:
        quality = "final-target-capable"
    elif reached["tp1"]:
        quality = "TP1-capable"
    elif mfe > 0:
        quality = "favourable but insufficient"
    else:
        quality = "adverse-first"
    row = {
        "strategy": record["strategy"], "symbol": record["symbol"], "direction": direction,
        "signal_timestamp": int(record["signal_timestamp"]), "fill_timestamp": fill_ts,
        "mfe": mfe, "mae": mae, "mfe_r": mfe / risk if risk else 0.0,
        "mae_r": mae / risk if risk else 0.0, "time_to_mfe": time_mfe,
        "time_to_mae": time_mae, "time_to_tp1": times["tp1"], "time_to_stop": stop_time,
        "favourable_first": bool(first_favourable and (not first_adverse or first_favourable < first_adverse)),
        "adverse_first": bool(first_adverse and (not first_favourable or first_adverse <= first_favourable)),
        "entry_quality": quality, **{f"reached_{key}": value for key, value in reached.items()},
    }
    for offset in (1, 2, 4, 8, 16):
        row[f"close_displacement_{offset}_r"] = _directional(direction, entry, path[offset - 1].close) / risk if len(path) >= offset and risk else None
    signal_index = next((i for i,c in enumerate(candles) if c.timestamp_ms == int(record["signal_timestamp"])), None)
    signal = candles[signal_index] if signal_index is not None else None
    signal_range = (signal.high - signal.low) if signal else 0.0
    recent = candles[max(0, signal_index-19):signal_index+1] if signal_index is not None else []
    true_ranges=[]
    for i,candle in enumerate(recent):
        previous_close=recent[i-1].close if i else candle.open
        true_ranges.append(max(candle.high-candle.low,abs(candle.high-previous_close),abs(candle.low-previous_close)))
    atr=mean(true_ranges[-14:]) if true_ranges else 0.0
    swing=(max(c.high for c in recent)-min(c.low for c in recent)) if recent else 0.0
    row.update({
        "stop_distance_pct": risk / entry * 100 if entry else 0.0,
        "stop_vs_signal_range": risk / signal_range if signal_range else None,
        "stop_distance_atr": risk / atr if atr else None,
        "stop_vs_recent_swing": risk / swing if swing else None,
        "mae_vs_stop": mae / risk if risk else None,
        "stopped_then_tp1": bool(stop_time and times["tp1"] and times["tp1"] > stop_time),
        "stopped_then_1r": bool(stop_time and times["r100"] and times["r100"] > stop_time),
        "stopped_then_final": bool(stop_time and times["final"] and times["final"] > stop_time),
        "initial_rr_tp1": abs(tp1 - entry) / risk if risk else 0.0,
        "initial_rr_final": abs(target2 - entry) / risk if risk else 0.0,
    })
    return row


def _fee(price: float, quantity: float, bps: float = 6.0) -> float:
    return price * quantity * bps / 10_000.0


def _pnl(direction: str, entry: float, exit_price: float, quantity: float) -> float:
    return (exit_price - entry) * quantity if direction == "LONG" else (entry - exit_price) * quantity


def _adverse_exit(direction: str, reference: float) -> float:
    return reference * (1.0 - 6.0 / 10_000.0) if direction == "LONG" else reference * (1.0 + 6.0 / 10_000.0)


def counterfactual(record: dict[str, str], candles: list[Candle], mode: str) -> dict[str, Any]:
    fill_ts = int(record["fill_timestamp"])
    start = next(i for i, candle in enumerate(candles) if candle.timestamp_ms == fill_ts)
    horizon = 16 if mode in {"time_16", "max_horizon"} else 8 if mode == "time_8" else 4 if mode == "time_4" else 6
    path = candles[start + 1:start + 1 + horizon]
    direction = record["direction"]
    entry, stop, tp1 = map(float, (record["executed_entry"], record["initial_stop"], record["tp1_price"]))
    requested = float(record["requested_entry"])
    tp2 = requested + (1.5 * abs(requested - stop) * (1 if direction == "LONG" else -1))
    quantity = float(record["initial_quantity"])
    remaining = quantity
    gross = 0.0
    fees = float(record["entry_fee"])
    tp1_done = False
    reason = "HORIZON"
    for index, candle in enumerate(path, 1):
        active_stop = stop
        if tp1_done:
            if mode == "partial_raw_be": active_stop = entry
            elif mode in {"partial_fee_be", "current"}: active_stop = float(record["stop_after_tp1"] or stop)
        stop_hit = candle.low <= active_stop if direction == "LONG" else candle.high >= active_stop
        target = tp2 if tp1_done else tp1
        target_hit = candle.high >= target if direction == "LONG" else candle.low <= target
        if stop_hit:
            executed=_adverse_exit(direction,active_stop);gross += _pnl(direction, entry, executed, remaining); fees += _fee(executed, remaining)
            remaining = 0; reason = "STOP" if not tp1_done else "POST_TP1_STOP"; break
        if target_hit:
            if mode == "full_tp1_sl":
                executed=_adverse_exit(direction,tp1);gross += _pnl(direction, entry, executed, remaining); fees += _fee(executed, remaining); remaining = 0; reason = "TP1"; break
            if not tp1_done:
                partial = math.floor(quantity * .4 / .001) * .001
                executed=_adverse_exit(direction,tp1);gross += _pnl(direction, entry, executed, partial); fees += _fee(executed, partial)
                remaining -= partial; tp1_done = True
                continue
            executed=_adverse_exit(direction,tp2);gross += _pnl(direction, entry, executed, remaining); fees += _fee(executed, remaining)
            remaining = 0; reason = "FINAL_TARGET"; break
        if mode.startswith("time_") and index == horizon:
            executed=_adverse_exit(direction,candle.close);gross += _pnl(direction, entry, executed, remaining); fees += _fee(executed, remaining)
            remaining = 0; reason = mode.upper(); break
    if remaining and path:
        executed=_adverse_exit(direction,path[-1].close);gross += _pnl(direction, entry, executed, remaining); fees += _fee(executed, remaining)
        reason = "MAX_HORIZON" if mode == "max_horizon" else "HORIZON"
    return {"strategy": record["strategy"], "mode": mode, "net_pnl": gross - fees, "gross_pnl": gross, "fees": fees, "reason": reason}


def wilson(wins: int, n: int, z: float = 1.96) -> list[float]:
    if not n: return [0.0, 0.0]
    p = wins / n; denominator = 1 + z*z/n
    centre = (p + z*z/(2*n))/denominator
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))/denominator
    return [centre-half, centre+half]


def uncertainty(values: list[float], seed: int = SEED) -> dict[str, Any]:
    if not values: return {"trades": 0, "status": "INSUFFICIENT_DATA"}
    rng = random.Random(seed)
    means, pfs, endings = [], [], []
    n = len(values)
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(mean(sample)); endings.append(1000 + sum(sample))
        gp = sum(v for v in sample if v > 0); gl = -sum(v for v in sample if v < 0)
        if gl: pfs.append(gp/gl)
    def interval(items):
        ordered = sorted(items); return [ordered[int(.025*(len(ordered)-1))], ordered[int(.975*(len(ordered)-1))]]
    wins = sum(v > 0 for v in values)
    return {
        "trades": n, "mean_net_pnl": mean(values), "mean_bootstrap_95": interval(means),
        "profit_factor_bootstrap_95": interval(pfs) if pfs else None,
        "win_rate": wins/n, "win_rate_wilson_95": wilson(wins, n),
        "pnl_without_best": sum(values)-max(values), "pnl_without_worst": sum(values)-min(values),
        "monte_carlo_ending_equity_95": interval(endings), "seed": seed, "samples": BOOTSTRAP_SAMPLES,
    }


def aggregate(rows: list[dict[str, Any]], keys: tuple[str, ...], excursion_lookup: dict[tuple, dict]) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(row[key] for key in keys)].append(row)
    output=[]
    for key, values in sorted(grouped.items()):
        pnl=[float(v["net_pnl"]) for v in values]; gp=sum(v for v in pnl if v>0); gl=-sum(v for v in pnl if v<0)
        ex=[excursion_lookup.get((v["strategy"],v["symbol"],int(v["signal_timestamp"]))) for v in values]; ex=[v for v in ex if v]
        output.append({**dict(zip(keys,key)),"trades":len(values),"gross_pnl":sum(float(v["gross_pnl"]) for v in values),"net_pnl":sum(pnl),"profit_factor":gp/gl if gl else 0.0,"expectancy":mean(pnl),"win_rate":sum(v>0 for v in pnl)/len(pnl),"average_mfe_r":mean([v["mfe_r"] for v in ex]) if ex else None,"average_mae_r":mean([v["mae_r"] for v in ex]) if ex else None})
    return output


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows: path.write_text(""); return
    with path.open("w", newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def gate_family(reason: str) -> str:
    value=reason.lower()
    if "proxy blocked: tp1" in value:return "PROXY_TP1_COST_FLOOR"
    if "proxy blocked: relative candle volume" in value:return "PROXY_VOLUME_FLOOR"
    if "proxy blocked: signal candle range" in value:return "PROXY_RANGE_CEILING"
    if "proxy blocked: volatility" in value:return "PROXY_VOLATILITY_CEILING"
    if "entry too high" in value or "entry too low" in value:return "ENTRY_POSITION"
    if "exhaustion" in value:return "MOMENTUM_EXHAUSTION"
    if "late breakout" in value or "late breakdown" in value:return "MOMENTUM_LATENESS"
    if "volume ratio too weak" in value:return "MOMENTUM_VOLUME"
    if "continuation blocked: weak volume" in value:return "CONTINUATION_VOLUME"
    if "score" in value:return "SCORE_THRESHOLD"
    if "alignment" in value or "trend" in value:return "HTF_TREND_ALIGNMENT"
    return "OTHER_OR_COUPLED"


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--proxy-report",type=Path,required=True);parser.add_argument("--structural-report",type=Path,required=True)
    parser.add_argument("--dataset",type=Path,required=True);parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    candles=load_candles(args.dataset); records=_csv(args.proxy_report/"trade_level.csv")
    closed=[r for r in records if r["fill_status"]=="FILLED" and r["final_exit_reason"] not in {"","OPEN_AT_DATA_END"}]
    excursions=[excursion(r,candles[r["symbol"]]) for r in closed]
    lookup={(r["strategy"],r["symbol"],int(r["signal_timestamp"])):r for r in excursions}
    write_csv(args.output/"entry_excursions.csv",excursions);write_json(args.output/"mfe_mae_distributions.json",{s:{"trades":len(v),"average_mfe_r":mean([x["mfe_r"] for x in v]) if v else 0,"average_mae_r":mean([x["mae_r"] for x in v]) if v else 0,"quality":dict(Counter(x["entry_quality"] for x in v))} for s in STRATEGIES for v in [[x for x in excursions if x["strategy"]==s]]})
    modes=("full_tp1_sl","partial_original_sl","partial_raw_be","partial_fee_be","time_4","time_8","time_16","max_horizon")
    cf=[counterfactual(r,candles[r["symbol"]],mode) for r in closed for mode in modes]
    cf_summary=[]
    for (strategy,mode),rows in sorted(defaultdict(list, {k:[x for x in cf if (x["strategy"],x["mode"])==k] for k in {(x["strategy"],x["mode"]) for x in cf}}).items()):
        vals=[x["net_pnl"] for x in rows];gp=sum(v for v in vals if v>0);gl=-sum(v for v in vals if v<0)
        running=peak=1000.0;max_dd=0.0
        for value in vals:running+=value;peak=max(peak,running);max_dd=max(max_dd,peak-running)
        cf_summary.append({"strategy":strategy,"mode":mode,"trades":len(rows),"gross_pnl":sum(x["gross_pnl"] for x in rows),"net_pnl":sum(vals),"profit_factor":gp/gl if gl else 0,"expectancy":mean(vals),"win_rate":sum(v>0 for v in vals)/len(vals),"average_win":mean([v for v in vals if v>0]) if any(v>0 for v in vals) else 0,"average_loss":mean([v for v in vals if v<0]) if any(v<0 for v in vals) else 0,"max_drawdown":max_dd,"tp1_exits":sum(x["reason"]=="TP1" for x in rows),"break_even_exits":sum(x["reason"]=="POST_TP1_STOP" for x in rows),"full_stops":sum(x["reason"]=="STOP" for x in rows),"final_targets":sum(x["reason"]=="FINAL_TARGET" for x in rows)})
    for strategy in STRATEGIES:
        rows=[r for r in closed if r["strategy"]==strategy]
        vals=[float(r["net_pnl"]) for r in rows];gp=sum(v for v in vals if v>0);gl=-sum(v for v in vals if v<0);running=peak=1000.0;max_dd=0.0
        for value in vals:running+=value;peak=max(peak,running);max_dd=max(max_dd,peak-running)
        cf_summary.append({"strategy":strategy,"mode":"current_frozen","trades":len(rows),"gross_pnl":sum(float(r["gross_pnl"]) for r in rows),"net_pnl":sum(vals),"profit_factor":gp/gl if gl else 0,"expectancy":mean(vals) if vals else 0,"win_rate":sum(v>0 for v in vals)/len(vals) if vals else 0,"average_win":mean([v for v in vals if v>0]) if any(v>0 for v in vals) else 0,"average_loss":mean([v for v in vals if v<0]) if any(v<0 for v in vals) else 0,"max_drawdown":max_dd,"tp1_exits":sum(float(r["tp1_quantity"])>0 for r in rows),"break_even_exits":sum(r["final_exit_reason"]=="BREAK_EVEN_STOP" for r in rows),"full_stops":sum(r["final_exit_reason"]=="STOP_LOSS" for r in rows),"final_targets":sum(r["final_exit_reason"] in {"FINAL_TARGET","TAKE_PROFIT"} for r in rows)})
    write_csv(args.output/"exit_counterfactuals.csv",cf_summary)
    write_csv(args.output/"stop_geometry.csv",[{k:v for k,v in row.items() if k in {"strategy","symbol","direction","signal_timestamp","stop_distance_pct","stop_distance_atr","stop_vs_signal_range","stop_vs_recent_swing","mae_vs_stop","stopped_then_tp1","stopped_then_1r","stopped_then_final"}} for row in excursions])
    write_csv(args.output/"target_reach.csv",[{k:v for k,v in row.items() if k.startswith("reached_") or k in {"strategy","symbol","direction","signal_timestamp","initial_rr_tp1","initial_rr_final","mfe_r"}} for row in excursions])
    cost=[]
    for strategy in STRATEGIES:
        rows=[r for r in closed if r["strategy"]==strategy];n=len(rows)
        execution_gross=sum(float(r["gross_pnl"]) for r in rows);entry_spread=sum(float(r["spread_cost"]) for r in rows);entry_slip=sum(float(r["entry_slippage"]) for r in rows);exit_adverse=sum(float(r["exit_slippage"]) for r in rows);fees=sum(float(r["total_fees"]) for r in rows)
        pure_gross=execution_gross+entry_spread+entry_slip+exit_adverse;total_cost=entry_spread+entry_slip+exit_adverse+fees;notional=sum(float(r["notional"]) for r in rows);net=sum(float(r["net_pnl"]) for r in rows)
        cost.append({"strategy":strategy,"trades":n,"price_movement_gross_pnl":pure_gross,"execution_adjusted_gross_pnl":execution_gross,"entry_spread":entry_spread,"exit_spread_estimate":exit_adverse*2/3,"entry_slippage":entry_slip,"exit_slippage_estimate":exit_adverse/3,"entry_fees":sum(float(r["entry_fee"]) for r in rows),"partial_exit_fees":sum(float(r["tp1_fee"]) for r in rows),"final_exit_fees":sum(float(r["final_exit_fee"]) for r in rows),"total_fees":fees,"total_transaction_cost":total_cost,"net_pnl":net,"gross_expectancy":pure_gross/n if n else 0,"net_expectancy":net/n if n else 0,"average_gross_edge_bps":pure_gross/notional*10000 if notional else 0,"average_round_trip_cost_bps":total_cost/notional*10000 if notional else 0,"gross_edge_to_cost":pure_gross/total_cost if total_cost else 0,"cost_pct_positive_gross_profit":total_cost/sum(max(0,float(r["gross_pnl"])+float(r["spread_cost"])+float(r["entry_slippage"])+float(r["exit_slippage"])) for r in rows)*100 if any(float(r["gross_pnl"])>0 for r in rows) else 0})
    write_csv(args.output/"cost_attribution.csv",cost)
    for name,keys in (("direction",("strategy","direction")),("symbol",("strategy","symbol"))):write_csv(args.output/f"{name}_breakdown.csv",aggregate(closed,keys,lookup))
    trade_regimes={(r["strategy"],r["symbol"],int(r["signal_timestamp"])):r.get("regime","UNAVAILABLE") for r in _json(args.proxy_report/"trade_log.json")}
    event_context={(item["strategy"],event["symbol"],int(item["signal_timestamp"])):{"volatility_bucket":event.get("volatility_bucket","UNAVAILABLE"),"subtype":item.get("subtype") or "UNAVAILABLE"} for event in _json(args.proxy_report/"candidate_events.json") for item in event["detectors"]}
    enriched=[]
    for row in closed:
        dt=__import__('datetime').datetime.fromtimestamp(int(row["signal_timestamp"])/1000,tz=__import__('datetime').timezone.utc);hour=dt.hour
        context=event_context.get((row["strategy"],row["symbol"],int(row["signal_timestamp"])),{})
        enriched.append({**row,"month":dt.strftime("%Y-%m"),"hour":str(hour),"session":"ASIA" if hour<8 else "EUROPE" if hour<16 else "US","regime":trade_regimes.get((row["strategy"],row["symbol"],int(row["signal_timestamp"])),"UNAVAILABLE"),"volatility_bucket":context.get("volatility_bucket","UNAVAILABLE"),"subtype":context.get("subtype","UNAVAILABLE")})
    for name,keys in (("calendar",("strategy","month")),("hour",("strategy","hour")),("session",("strategy","session")),("regime",("strategy","regime")),("volatility",("strategy","volatility_bucket")),("subtype",("strategy","subtype"))):write_csv(args.output/f"{name}_breakdown.csv",aggregate(enriched,keys,lookup))
    candidate_events=_json(args.proxy_report/"candidate_events.json");combo=Counter();winners=Counter();sweep_nonselected=Counter();multi=0
    nonselected=[];execution=BacktestExecutionContract(BacktestExecutionConfig())
    for event in candidate_events:
        names=tuple(sorted(item["strategy"] for item in event["detectors"]));combo["+".join(names)]+=1
        if len(names)>1:multi+=1
        if event["selected_strategy"]:winners[event["selected_strategy"]]+=1
        if "liquidity_sweep_reversal" in names and event["selected_strategy"]!="liquidity_sweep_reversal":sweep_nonselected[event["selected_strategy"] or "NONE"]+=1
        series=candles[event["symbol"]];start=next((i for i,c in enumerate(series) if c.timestamp_ms==int(event["snapshot_as_of_timestamp"])),None)
        if start is None:continue
        for item in event["detectors"]:
            if item["candidate_id"]==event["selected_candidate_id"]:continue
            entry=float(item["entry"]);stop=float(item["stop"]);risk=abs(entry-stop)
            targets=[entry+risk*.8*(1 if item["direction"]=="LONG" else -1),entry+risk*1.5*(1 if item["direction"]=="LONG" else -1)]
            rec=execution.execute(strategy=item["strategy"],symbol=event["symbol"],timeframe="15m",direction=item["direction"],signal_timestamp=int(item["signal_timestamp"]),requested_entry=entry,stop=stop,targets=targets,candles=series[start:start+7],equity=1000,risk_policy="SELECTOR_COUNTERFACTUAL")
            nonselected.append({"strategy":item["strategy"],"symbol":event["symbol"],"direction":item["direction"],"signal_timestamp":item["signal_timestamp"],"selected_winner":event["selected_strategy"] or "NONE","selector_reason":event["selector_reason"],"fill_status":rec.fill_status,"final_exit_reason":rec.final_exit_reason,"net_pnl":rec.net_pnl})
    write_csv(args.output/"nonselected_counterfactuals.csv",nonselected)
    sweep_cf=[r for r in nonselected if r["strategy"]=="liquidity_sweep_reversal" and r["final_exit_reason"] not in {"","OPEN_AT_DATA_END"}]
    competition={"one_detector_snapshots":sum(v for k,v in combo.items() if "+" not in k),"multiple_detector_snapshots":multi,"combinations":dict(combo),"selected_winners":dict(winners),"sweep_nonselected_winners":dict(sweep_nonselected),"nonselected_counterfactual_count":len(nonselected),"sweep_nonselected_closed":len(sweep_cf),"sweep_nonselected_net_pnl":sum(float(r["net_pnl"]) for r in sweep_cf)};write_json(args.output/"selector_competition.json",competition)
    decisions=_json(args.proxy_report/"risk_gate_decisions.json");structural=_json(args.structural_report/"risk_gate_decisions.json")
    gates=[]
    for source,label in ((structural,"structural"),(decisions,"proxy")):
        counts=Counter(reason for row in source if not row["allowed"] for reason in row["reasons"] if "blocked" in reason.lower())
        gates.extend({"stage":label,"gate":reason,"blocked":count,"classification":"IMPOSSIBLE_TO_ISOLATE" if label=="structural" else "SEE_PHASE2C_GATE_VALUE"} for reason,count in counts.most_common())
    write_csv(args.output/"gate_attribution.csv",gates)
    item_lookup={
        (item["strategy"],event["symbol"],item["direction"],int(item["signal_timestamp"])):(event,item)
        for event in candidate_events for item in event["detectors"]
    }
    blocked_rows=[]
    for stage,source in (("structural",structural),("proxy",decisions)):
        for decision in source:
            if decision["allowed"]:continue
            pair=item_lookup.get((decision["strategy"],decision["symbol"],decision["direction"],int(decision["signal_timestamp"])))
            if not pair:continue
            event,item=pair;series=candles[event["symbol"]];start=next((i for i,c in enumerate(series) if c.timestamp_ms==int(event["snapshot_as_of_timestamp"])),None)
            if start is None:continue
            entry=float(item["entry"]);stop=float(item["stop"]);risk=abs(entry-stop);targets=[entry+risk*.8*(1 if item["direction"]=="LONG" else -1),entry+risk*1.5*(1 if item["direction"]=="LONG" else -1)]
            rec=execution.execute(strategy=item["strategy"],symbol=event["symbol"],timeframe="15m",direction=item["direction"],signal_timestamp=int(item["signal_timestamp"]),requested_entry=entry,stop=stop,targets=targets,candles=series[start:start+7],equity=1000,risk_policy="BLOCKED_GATE_COUNTERFACTUAL")
            families=sorted({gate_family(reason) for reason in decision["reasons"] if "blocked" in reason.lower()})
            for family in families:blocked_rows.append({"stage":stage,"gate":family,"strategy":item["strategy"],"gate_family_count":len(families),"fill_status":rec.fill_status,"final_exit_reason":rec.final_exit_reason,"net_pnl":rec.net_pnl})
    write_csv(args.output/"blocked_gate_counterfactuals.csv",blocked_rows)
    gate_values=[]
    for (stage,family,strategy),rows in sorted(defaultdict(list,{k:[x for x in blocked_rows if (x["stage"],x["gate"],x["strategy"])==k] for k in {(x["stage"],x["gate"],x["strategy"]) for x in blocked_rows}}).items()):
        exclusive=[x for x in rows if int(x["gate_family_count"])==1];closed_gate=[x for x in exclusive if x["final_exit_reason"] not in {"","OPEN_AT_DATA_END"}];vals=[float(x["net_pnl"]) for x in closed_gate];gp=sum(v for v in vals if v>0);gl=-sum(v for v in vals if v<0)
        classification="ADDS_MEASURABLE_VALUE" if len(vals)>=10 and sum(vals)<0 else "DESTROYS_MEASURABLE_VALUE" if len(vals)>=10 and sum(vals)>0 else "INSUFFICIENT_SAMPLE" if exclusive else "IMPOSSIBLE_TO_ISOLATE"
        gate_values.append({"stage":stage,"gate":family,"strategy":strategy,"blocked_candidates":len(rows),"exclusive_candidates":len(exclusive),"hypothetical_closed":len(closed_gate),"hypothetical_net_pnl":sum(vals),"hypothetical_profit_factor":gp/gl if gl else 0,"hypothetical_expectancy":mean(vals) if vals else 0,"classification":classification})
    write_csv(args.output/"gate_value_diagnostics.csv",gate_values)
    uncertainty_rows={s:uncertainty([float(r["net_pnl"]) for r in closed if r["strategy"]==s]) for s in STRATEGIES};write_json(args.output/"statistical_uncertainty.json",uncertainty_rows)
    funnel={s:{"detector_true":sum(sum(i["strategy"]==s for i in e["detectors"]) for e in candidate_events),"selected":sum(e["selected_strategy"]==s for e in candidate_events),"structural_accepted":sum(r["strategy"]==s and r["allowed"] for r in structural),"proxy_accepted":sum(r["strategy"]==s and r["allowed"] for r in decisions),"execution_attempted":sum(r["strategy"]==s for r in records),"rejected":sum(r["strategy"]==s and r["fill_status"]=="REJECTED" for r in records),"filled":sum(r["strategy"]==s and r["fill_status"]=="FILLED" for r in records),"closed":sum(r["strategy"]==s for r in closed)} for s in STRATEGIES};write_json(args.output/"funnel_attrition.json",funnel)
    verdicts={"trend_continuation":{"failure":"ENTRY FAILURE","decision":"REJECT STRATEGY AS CURRENTLY DEFINED"},"liquidity_sweep_reversal":{"failure":"INSUFFICIENT SAMPLE","decision":"EXPAND SAMPLE WITHOUT CHANGING LOGIC"},"momentum_breakdown":{"failure":"ENTRY FAILURE"},"momentum_breakout":{"failure":"INSUFFICIENT SAMPLE"},"low_vol_reclaim":{"failure":"INSUFFICIENT SAMPLE"},"adaptive_momentum_continuation":{"failure":"INSUFFICIENT SAMPLE"}};write_json(args.output/"strategy_verdicts.json",verdicts)
    contract={"execution_commit":"2009a4a5cc8525436df8fb4e09c93a5b2bd237f2","phase2a_commit":"235274958ff2a68052b9b43a8cddb6380478fcc4","phase2b_commit":"523fc77","phase2c_commit":"2ad6f73","dataset_hash":"9053781ed26065ebb6cc693cfd363fd5f784493488916ab319675cbf199a0f76","proxy_hash":"722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc","seed":SEED,"bootstrap_samples":BOOTSTRAP_SAMPLES,"official_baseline_hash":"2d898466d079c8c6db34e468a6fb6a0f391f6e93539802ecd8be758f0083fa44"};write_json(args.output/"diagnosis_contract.json",contract)
    digest=hashlib.sha256(b"".join(p.read_bytes() for p in sorted(args.output.iterdir()) if p.name!="diagnosis_hash.txt")).hexdigest();(args.output/"diagnosis_hash.txt").write_text(digest+"\n")
    return 0

if __name__=="__main__":raise SystemExit(main())
