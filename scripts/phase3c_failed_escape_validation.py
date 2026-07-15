from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from clients.schemas import Candle
from research.failed_range_escape_reversal_v1 import (
    FINAL_R, MAX_HOLD_CANDLES, STRATEGY, TP1_R, detect_at,
    hourly_context_series, validate_candles,
)
from research.preregistration_protocol import (
    DEVELOPMENT_END_MS, DEVELOPMENT_START_MS, VALIDATION_END_MS,
    VALIDATION_START_MS, preregistration_hash, validate_preregistration,
)

SEED = 20260715
RESAMPLES = 5000
PREREGISTRATION_HASH = "e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9"
DATASET_HASHES = {
    "development": "d7d7a7670b6bd5723cc5f0b7b279b099c3b0258659f2cfd384c9b9179b0953fb",
    "validation": "9053781ed26065ebb6cc693cfd363fd5f784493488916ab319675cbf199a0f76",
}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def load_candles(path: Path) -> dict[str, list[Candle]]:
    result = {}
    for source in sorted(path.glob("*.json")):
        payload = json.loads(source.read_text())
        rows = payload.get("candles", payload) if isinstance(payload, dict) else payload
        result[source.stem.upper()] = [Candle(
            int(row.get("timestamp_ms", row.get("timestamp"))), float(row["open"]),
            float(row["high"]), float(row["low"]), float(row["close"]),
            float(row["volume_base"]),
            float(row["volume_quote"]) if row.get("volume_quote") is not None else None,
        ) for row in rows]
    return result


def verify_contract(preregistration: Path) -> dict[str, Any]:
    document = json.loads(preregistration.read_text())
    validate_preregistration(document)
    if preregistration_hash(document) != PREREGISTRATION_HASH:
        raise ValueError("TECHNICALLY INVALID — PREREGISTRATION MISMATCH")
    return document


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row}) if rows else ["empty"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _step(value: float, step: float, up: bool) -> float:
    units = Decimal(str(value)) / Decimal(str(step))
    return float(units.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * Decimal(str(step)))


def predicted_market_entry(reference: float, direction: str, config: BacktestExecutionConfig) -> float:
    buy = direction == "LONG"
    bps = config.spread_bps + config.entry_slippage_bps
    raw = reference * (1 + bps / 10_000) if buy else reference * (1 - bps / 10_000)
    return _step(raw, config.price_tick, buy)


def adverse_exit(reference: float, direction: str, config: BacktestExecutionConfig) -> float:
    sell = direction == "LONG"
    bps = config.spread_bps + config.exit_slippage_bps
    raw = reference * (1 - bps / 10_000) if sell else reference * (1 + bps / 10_000)
    return _step(raw, config.price_tick, not sell)


def finalize_locked_eighth_candle(record, candle: Candle, config: BacktestExecutionConfig) -> None:
    """Close a remainder when TP1 occurs on the eighth and final hold candle.

    The frozen generic contract returns OPEN_AT_DATA_END in this exact branch
    because TP1 handling continues before its time-exit clause. This research-
    only completion uses the same adverse price, taker fee and PnL formulas.
    """
    if not (
        record.final_exit_reason == "OPEN_AT_DATA_END"
        and record.candles_held == MAX_HOLD_CANDLES
        and record.tp1_quantity > 0
    ):
        return
    remaining = record.initial_quantity - record.tp1_quantity
    if remaining <= 0:
        return
    executed = adverse_exit(candle.close, record.direction, config)
    fee = executed * remaining * config.taker_fee_bps / 10_000
    tp1_gross = (
        (record.tp1_executed_price - record.executed_entry) * record.tp1_quantity
        if record.direction == "LONG" else (record.executed_entry - record.tp1_executed_price) * record.tp1_quantity
    )
    final_gross = (
        (executed - record.executed_entry) * remaining
        if record.direction == "LONG" else (record.executed_entry - executed) * remaining
    )
    record.final_exit_price = executed
    record.final_exit_fee = fee
    record.exit_slippage += abs(executed - candle.close) * remaining
    record.final_exit_reason = "TIME_EXIT"
    record.timed_exit = True
    record.gross_pnl = tp1_gross + final_gross
    record.total_fees = record.entry_fee + record.tp1_fee + fee
    record.net_pnl = record.gross_pnl - record.total_fees
    record.equity_after = record.equity_before + record.net_pnl
    record.r_multiple = record.net_pnl / record.risk_budget if record.risk_budget else 0.0


def scan(path: Path, start_ms: int, end_ms: int) -> tuple[dict[str, list[Candle]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    data = load_candles(path)
    decisions: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    funnel = defaultdict(int)
    for symbol, candles in data.items():
        if candles[0].timestamp_ms != start_ms or candles[-1].timestamp_ms + 900_000 != end_ms:
            raise ValueError(f"dataset boundary mismatch: {symbol}")
        funnel["snapshots"] += len(candles)
        validate_candles(candles)
        htf_contexts = hourly_context_series(candles)
        for index in range(len(candles) - 1):
            decision = detect_at(symbol, candles, index, validated=True, htf_context=htf_contexts[index])
            if decision.status in {"RAW_ESCAPE", "VALID_REENTRY", "CANDIDATE"}:
                funnel["raw_failed_escapes"] += 1
            if decision.status in {"VALID_REENTRY", "CANDIDATE"}:
                funnel["valid_reentries"] += 1
            if decision.reason == "INSUFFICIENT_HISTORY":
                funnel["insufficient_history"] += 1
            if decision.reason == "STOP_DISTANCE_GT_2_ATR":
                funnel["stop_distance_rejected"] += 1
            if decision.reason == "TP1_DISTANCE_LT_72_BPS":
                funnel["cost_distance_rejected"] += 1
            if decision.candidate and decision.status == "CANDIDATE":
                row = decision.candidate.to_dict()
                row["reentry_index"] = index
                decisions.append(row)
                funnel[f"{decision.candidate.direction.lower()}_candidates"] += 1
            elif decision.candidate:
                rejections.append({**decision.candidate.to_dict(), "rejection_reason": decision.reason})
    order = lambda row: (row["entry_timestamp_ms"], row["symbol"], row["direction"])
    return data, sorted(decisions, key=order), sorted(rejections, key=order), dict(funnel)


def execute_candidates(
    data: dict[str, list[Candle]], candidates: list[dict[str, Any]], funnel: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    config = replace(BacktestExecutionConfig(), max_hold_candles=MAX_HOLD_CANDLES)
    contract = BacktestExecutionContract(config)
    equity = config.starting_equity
    records: list[dict[str, Any]] = []
    suppressions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    active_until: dict[str, int] = {}
    indices = {symbol: {c.timestamp_ms: i for i, c in enumerate(candles)} for symbol, candles in data.items()}
    for candidate in candidates:
        identity = (STRATEGY, candidate["symbol"], candidate["direction"], candidate["signal_timestamp_ms"])
        if identity in seen:
            funnel["duplicate_suppressed"] = funnel.get("duplicate_suppressed", 0) + 1
            suppressions.append({**candidate, "suppression_reason": "DUPLICATE_CANDIDATE"})
            continue
        seen.add(identity)
        entry_index = indices[candidate["symbol"]][candidate["entry_timestamp_ms"]]
        if active_until.get(candidate["symbol"], -1) >= entry_index:
            funnel["active_overlap_suppressed"] = funnel.get("active_overlap_suppressed", 0) + 1
            suppressions.append({**candidate, "suppression_reason": "ACTIVE_SAME_SYMBOL"})
            continue
        series = data[candidate["symbol"]]
        executed_entry = predicted_market_entry(candidate["requested_entry"], candidate["direction"], config)
        risk = abs(executed_entry - candidate["stop"])
        targets = [
            executed_entry + (TP1_R * risk if candidate["direction"] == "LONG" else -TP1_R * risk),
            executed_entry + (FINAL_R * risk if candidate["direction"] == "LONG" else -FINAL_R * risk),
        ]
        record = contract.execute(
            strategy=STRATEGY, symbol=candidate["symbol"], timeframe="15m",
            direction=candidate["direction"], signal_timestamp=candidate["signal_timestamp_ms"],
            requested_entry=candidate["requested_entry"], stop=candidate["stop"], targets=targets,
            candles=series[entry_index: entry_index + MAX_HOLD_CANDLES + 2], equity=equity,
            risk_policy="PREREGISTERED_RESEARCH_ONLY",
        )
        if entry_index + MAX_HOLD_CANDLES < len(series):
            finalize_locked_eighth_candle(record, series[entry_index + MAX_HOLD_CANDLES], config)
        row = {**candidate, **asdict(record)}
        row["gap_from_signal_close"] = candidate["requested_entry"] - candidate["signal_close"]
        row["entry_spread_bps"] = config.spread_bps
        row["entry_slippage_bps"] = config.entry_slippage_bps
        row["final_target_price"] = targets[1]
        row["close_timestamp_ms"] = (
            series[entry_index + record.candles_held].timestamp_ms
            if record.candles_held and entry_index + record.candles_held < len(series) else None
        )
        records.append(row)
        funnel["execution_attempted"] = funnel.get("execution_attempted", 0) + 1
        if record.fill_status != "FILLED":
            funnel["execution_rejected"] = funnel.get("execution_rejected", 0) + 1
        else:
            funnel["filled"] = funnel.get("filled", 0) + 1
            if record.final_exit_reason == "OPEN_AT_DATA_END":
                funnel["unresolved"] = funnel.get("unresolved", 0) + 1
            else:
                funnel["closed"] = funnel.get("closed", 0) + 1
                equity = record.equity_after
                active_until[candidate["symbol"]] = entry_index + record.candles_held
    for key in ("duplicate_suppressed", "active_overlap_suppressed", "execution_rejected", "filled", "closed", "unresolved"):
        funnel.setdefault(key, 0)
    return records, suppressions, funnel


def excursions(row: dict[str, Any], candles: list[Candle]) -> dict[str, Any]:
    index = next(i for i, candle in enumerate(candles) if candle.timestamp_ms == row["fill_timestamp"])
    path = candles[index + 1:index + 1 + MAX_HOLD_CANDLES]
    direction = row["direction"]
    entry = row["executed_entry"]
    risk = abs(entry - row["initial_stop"])
    favourable = [(c.high - entry if direction == "LONG" else entry - c.low) for c in path]
    adverse = [(entry - c.low if direction == "LONG" else c.high - entry) for c in path]
    adverse_first = False
    for candle in path:
        favourable_touch = candle.high > entry if direction == "LONG" else candle.low < entry
        adverse_touch = candle.low < entry if direction == "LONG" else candle.high > entry
        if favourable_touch or adverse_touch:
            adverse_first = adverse_touch
            break
    return {
        "mfe_r": max([0.0] + favourable) / risk,
        "mae_r": max([0.0] + adverse) / risk,
        "adverse_first": adverse_first,
    }


def closed(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in records if row["fill_status"] == "FILLED" and row["final_exit_reason"] != "OPEN_AT_DATA_END"]


def metrics(records: list[dict[str, Any]], data: dict[str, list[Candle]]) -> dict[str, Any]:
    rows = closed(records)
    pnl = [row["net_pnl"] for row in rows]
    gross = [row["gross_pnl"] for row in rows]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    positive = sum(wins); negative = -sum(losses)
    entry_fees = sum(row["entry_fee"] for row in rows)
    tp1_fees = sum(row["tp1_fee"] for row in rows)
    final_fees = sum(row["final_exit_fee"] for row in rows)
    spread = sum(row["spread_cost"] + row["exit_slippage"] * 2 / 3 for row in rows)
    slippage = sum(row["entry_slippage"] + row["exit_slippage"] / 3 for row in rows)
    gross_price = sum(gross) + spread + slippage
    total_costs = entry_fees + tp1_fees + final_fees + spread + slippage
    equity = peak = 1000.0; max_dd = 0.0
    for value in pnl:
        equity += value; peak = max(peak, equity); max_dd = max(max_dd, peak - equity)
    ex = [excursions(row, data[row["symbol"]]) for row in rows]
    reasons = lambda reason: sum(row["final_exit_reason"] == reason for row in rows) / len(rows) if rows else 0.0
    days = {datetime.fromtimestamp(row["fill_timestamp"] / 1000, timezone.utc).date().isoformat() for row in rows}
    return {
        "closed_trades": len(rows), "independent_trading_days": len(days),
        "gross_price_pnl": gross_price, "execution_adjusted_gross_pnl": sum(gross),
        "entry_fees": entry_fees, "tp1_fees": tp1_fees, "final_exit_fees": final_fees,
        "spread_impact": spread, "slippage_impact": slippage, "total_costs": total_costs,
        "net_pnl": sum(pnl), "total_return_pct": sum(pnl) / 1000 * 100,
        "ending_equity": 1000 + sum(pnl), "profit_factor": positive / negative if negative else None,
        "net_expectancy": mean(pnl) if pnl else 0.0,
        "gross_expectancy": mean(gross_price / len(rows) for _ in [0]) if rows else 0.0,
        "expectancy_r": mean(row["r_multiple"] for row in rows) if rows else 0.0,
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "payoff_ratio": mean(wins) / abs(mean(losses)) if wins and losses else None,
        "average_win": mean(wins) if wins else 0.0, "average_loss": mean(losses) if losses else 0.0,
        "median_trade": median(pnl) if pnl else 0.0, "best_trade": max(pnl) if pnl else 0.0,
        "worst_trade": min(pnl) if pnl else 0.0, "maximum_drawdown": max_dd,
        "maximum_drawdown_pct": max_dd / peak * 100 if peak else 0.0,
        "average_holding_candles": mean(row["candles_held"] for row in rows) if rows else 0.0,
        "tp1_hit_rate": sum(row["tp1_quantity"] > 0 for row in rows) / len(rows) if rows else 0.0,
        "break_even_exit_rate": reasons("BREAK_EVEN_STOP"), "final_target_rate": reasons("FINAL_TARGET"),
        "full_stop_rate": reasons("STOP_LOSS"), "maximum_hold_exit_rate": reasons("TIME_EXIT"),
        "average_mfe_r": mean(row["mfe_r"] for row in ex) if ex else 0.0,
        "average_mae_r": mean(row["mae_r"] for row in ex) if ex else 0.0,
        "adverse_first_pct": mean(row["adverse_first"] for row in ex) * 100 if ex else 0.0,
    }


def _segment_metrics(rows: list[dict[str, Any]], data: dict[str, list[Candle]]) -> dict[str, Any]:
    result = metrics(rows, data)
    return {key: result[key] for key in (
        "closed_trades", "gross_price_pnl", "net_pnl", "profit_factor", "net_expectancy",
        "win_rate", "average_mfe_r", "average_mae_r",
    )}


def breakdowns(records: list[dict[str, Any]], data: dict[str, list[Candle]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in closed(records):
        dt = datetime.fromtimestamp(row["signal_timestamp_ms"] / 1000, timezone.utc)
        hour = dt.hour
        session = "ASIA" if hour < 8 else "EUROPE" if hour < 16 else "US"
        atr_pct = row["atr14"] / row["signal_close"] * 100
        dimensions = {
            "direction": row["direction"], "symbol": row["symbol"], "month": dt.strftime("%Y-%m"),
            "utc_hour": str(hour), "session": session, "htf_context": row["htf_relationship"],
            "volatility_regime": "LOW" if atr_pct < 0.35 else "MEDIUM" if atr_pct < 0.75 else "HIGH",
            "stop_distance_bucket": "LE_1_ATR" if row["stop_distance_atr"] <= 1 else "LE_1_5_ATR" if row["stop_distance_atr"] <= 1.5 else "GT_1_5_ATR",
            "escape_distance_bucket": "LE_0_25_ATR" if row["escape_distance_atr"] <= .25 else "LE_0_5_ATR" if row["escape_distance_atr"] <= .5 else "GT_0_5_ATR",
        }
        for dimension, value in dimensions.items():
            groups[(dimension, value)].append(row)
    return [{"dimension": dimension, "value": value, "candidates": len(rows), **_segment_metrics(rows, data)} for (dimension, value), rows in sorted(groups.items())]


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values); position = (len(ordered) - 1) * probability
    low = math.floor(position); high = math.ceil(position)
    return ordered[low] if low == high else ordered[low] * (high - position) + ordered[high] * (position - low)


def statistics(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = closed(records); pnl = [row["net_pnl"] for row in rows]; r_values = [row["r_multiple"] for row in rows]
    if not rows:
        return {"seed": SEED, "resamples": RESAMPLES, "bootstrap_mean_net_ci95": [0, 0], "bootstrap_mean_r_ci95": [0, 0]}
    rng = random.Random(SEED); means=[]; mean_r=[]; pfs=[]; endings=[]; drawdowns=[]
    for _ in range(RESAMPLES):
        sample = [rng.randrange(len(rows)) for _ in rows]
        values = [pnl[i] for i in sample]; rs = [r_values[i] for i in sample]
        means.append(mean(values)); mean_r.append(mean(rs)); endings.append(1000 + sum(values))
        gains=sum(max(0,x) for x in values); losses=-sum(min(0,x) for x in values)
        if losses: pfs.append(gains/losses)
        equity=peak=1000.0; dd=0.0
        for value in values: equity+=value;peak=max(peak,equity);dd=max(dd,peak-equity)
        drawdowns.append(dd)
    wins=sum(value>0 for value in pnl);z=1.959963984540054;n=len(pnl);center=(wins/n+z*z/(2*n))/(1+z*z/n);half=z*math.sqrt((wins/n*(1-wins/n)+z*z/(4*n))/n)/(1+z*z/n)
    return {
        "seed": SEED, "resamples": RESAMPLES,
        "bootstrap_mean_net_ci95": [quantile(means,.025),quantile(means,.975)],
        "bootstrap_mean_r_ci95": [quantile(mean_r,.025),quantile(mean_r,.975)],
        "bootstrap_pf_ci95": [quantile(pfs,.025),quantile(pfs,.975)] if pfs else None,
        "wilson_win_rate_ci95": [max(0,center-half),min(1,center+half)],
        "monte_carlo_ending_equity": {"q05":quantile(endings,.05),"median":quantile(endings,.5),"q95":quantile(endings,.95)},
        "monte_carlo_max_drawdown": {"q05":quantile(drawdowns,.05),"median":quantile(drawdowns,.5),"q95":quantile(drawdowns,.95)},
        "without_best": sum(pnl)-max(pnl), "without_worst": sum(pnl)-min(pnl),
    }


def cost_dependency(records: list[dict[str, Any]], performance: dict[str, Any]) -> dict[str, Any]:
    rows=closed(records); pnl=[row["net_pnl"] for row in rows];gross_profit=sum(max(0,row["gross_pnl"]) for row in rows)
    by_symbol=defaultdict(float);by_month=defaultdict(float);by_direction=defaultdict(float)
    for row in rows:
        by_symbol[row["symbol"]]+=row["net_pnl"]
        month=datetime.fromtimestamp(row["signal_timestamp_ms"]/1000,timezone.utc).strftime("%Y-%m")
        by_month[month]+=row["net_pnl"];by_direction[row["direction"]]+=row["net_pnl"]
    positive_symbol=max([value for value in by_symbol.values() if value>0],default=0.0)
    positive_month=max([value for value in by_month.values() if value>0],default=0.0)
    total_positive_symbols=sum(max(0,value) for value in by_symbol.values());total_positive_months=sum(max(0,value) for value in by_month.values())
    top_count=max(1,math.ceil(len(pnl)*.10)) if pnl else 0
    return {
        "average_gross_price_edge_per_trade":performance["gross_price_pnl"]/len(rows) if rows else 0,
        "average_transaction_cost_per_trade":performance["total_costs"]/len(rows) if rows else 0,
        "edge_to_cost_ratio":performance["gross_price_pnl"]/performance["total_costs"] if performance["total_costs"] else None,
        "costs_pct_gross_profit":performance["total_costs"]/gross_profit*100 if gross_profit else None,
        "without_best_trade":sum(pnl)-max(pnl) if pnl else 0,"without_best_three":sum(pnl)-sum(sorted(pnl)[-3:]) if pnl else 0,
        "without_worst_trade":sum(pnl)-min(pnl) if pnl else 0,
        "best_symbol_positive_profit_share":positive_symbol/total_positive_symbols if total_positive_symbols else None,
        "best_month_positive_profit_share":positive_month/total_positive_months if total_positive_months else None,
        "direction_contribution":dict(sorted(by_direction.items())),
        "top_10pct_trade_contribution":sum(sorted(pnl,reverse=True)[:top_count])/sum(pnl) if pnl and sum(pnl)!=0 else None,
    }


def manual_reconciliation(candidates: list[dict[str, Any]], records: list[dict[str, Any]], suppressions: list[dict[str, Any]], rejections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    examples=[];by_direction={direction:[row for row in records if row["direction"]==direction][:3] for direction in ("LONG","SHORT")}
    for direction in ("LONG","SHORT"): examples.extend({**row,"manual_category":f"{direction}_SETUP"} for row in by_direction[direction])
    for reason,category in (("BREAK_EVEN_STOP","TP1_PLUS_BREAK_EVEN"),("STOP_LOSS","FULL_STOP"),("TIME_EXIT","MAXIMUM_HOLD")):
        row=next((item for item in records if item["final_exit_reason"]==reason),None)
        if row: examples.append({**row,"manual_category":category})
    if suppressions: examples.append({**suppressions[0],"manual_category":"SUPPRESSION"})
    stop_rejection=next((row for row in rejections if row["rejection_reason"]=="STOP_DISTANCE_GT_2_ATR"),None)
    if stop_rejection:examples.append({**stop_rejection,"manual_category":"STOP_DISTANCE_REJECTION"})
    return examples


def artifact_hash(output: Path) -> str:
    return hashlib.sha256(b"".join(path.read_bytes() for path in sorted(output.iterdir()) if path.name != "artifact_hash.txt")).hexdigest()


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--mode",choices=("reconcile","evaluate"),required=True)
    parser.add_argument("--period",choices=("development","validation"),required=True);parser.add_argument("--canonical",type=Path,required=True)
    parser.add_argument("--preregistration",type=Path,required=True);parser.add_argument("--implementation-manifest",type=Path)
    parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    verify_contract(args.preregistration)
    if args.mode=="reconcile" and args.period!="development":raise SystemExit("validation locked before implementation freeze")
    if args.mode=="evaluate":
        if not args.implementation_manifest:raise SystemExit("implementation manifest required")
        manifest=json.loads(args.implementation_manifest.read_text());stored=manifest.pop("implementation_hash",None)
        if stored!=canonical_hash(manifest):raise SystemExit("implementation manifest hash mismatch")
    if args.output.exists():raise SystemExit("refusing to overwrite Phase 3C artifact")
    start,end=(DEVELOPMENT_START_MS,DEVELOPMENT_END_MS) if args.period=="development" else (VALIDATION_START_MS,VALIDATION_END_MS)
    data,candidates,rejections,funnel=scan(args.canonical,start,end);records,suppressions,funnel=execute_candidates(data,candidates,funnel)
    args.output.mkdir(parents=True);write_json(args.output/"funnel.json",funnel);write_csv(args.output/"candidate_records.csv",candidates)
    write_csv(args.output/"rejection_records.csv",rejections);write_csv(args.output/"trade_records.csv",records);write_csv(args.output/"suppressions.csv",suppressions)
    write_csv(args.output/"manual_reconciliation.csv",manual_reconciliation(candidates,records,suppressions,rejections))
    if args.mode=="evaluate":
        performance=metrics(records,data);write_json(args.output/"performance.json",performance)
        write_json(args.output/"breakdowns.json",breakdowns(records,data));write_json(args.output/"cost_dependency.json",cost_dependency(records,performance))
        write_json(args.output/"statistical_uncertainty.json",statistics(records))
    digest=artifact_hash(args.output);(args.output/"artifact_hash.txt").write_text(digest+"\n");print(digest);return 0


if __name__ == "__main__":
    raise SystemExit(main())
