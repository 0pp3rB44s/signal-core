"""Strict diagnostic validation for the two frozen speculative candidates.

Historical results remain diagnostic because delisted-universe membership and
event-time books are absent. Prospective observations after the spec freeze are
the only records eligible to satisfy SHADOW_READY gates.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = Path(__file__).with_name("FROZEN_SPECS.json")
RESULTS = ROOT / "research/results/speculative_v1"
CACHE = ROOT / "research/data/speculative_v1"


def spec_hash() -> str:
    return hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest()


@lru_cache(maxsize=None)
def candle_frame(symbol: str) -> pd.DataFrame:
    path = CACHE / f"{symbol}_1H_180d.json"
    if not path.exists():
        path = CACHE / f"{symbol}_1H_1000.json"
    rows = json.loads(path.read_text())
    frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "base_volume", "quote_volume"])
    for col in frame.columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.sort_values("ts").drop_duplicates("ts").set_index("ts")


def path_return(symbol: str, signal_ts: int, direction: float, hold_h: int = 24,
                stop_fraction: float = .10) -> dict | None:
    """Next-open entry with gap-through-stop handling on hourly OHLC."""
    candles = candle_frame(symbol)
    after = candles.loc[candles.index > signal_ts]
    if len(after) < hold_h + 1:
        return None
    entry_row = after.iloc[0]
    entry_ts = int(after.index[0])
    entry = float(entry_row.open)
    path = after.iloc[:hold_h]
    stop = entry * (1 - stop_fraction) if direction > 0 else entry * (1 + stop_fraction)
    exit_price = float(after.iloc[hold_h].open)
    exit_ts = int(after.index[hold_h])
    stop_hit = False
    gap_bps = 0.0
    for ts, bar in path.iterrows():
        if direction > 0:
            if bar.open <= stop:
                exit_price, exit_ts, stop_hit = float(bar.open), int(ts), True
                gap_bps = max(0.0, (stop - exit_price) / entry * 1e4)
                break
            if bar.low <= stop:
                exit_price, exit_ts, stop_hit = stop, int(ts), True
                break
        else:
            if bar.open >= stop:
                exit_price, exit_ts, stop_hit = float(bar.open), int(ts), True
                gap_bps = max(0.0, (exit_price - stop) / entry * 1e4)
                break
            if bar.high >= stop:
                exit_price, exit_ts, stop_hit = stop, int(ts), True
                break
    gross_bps = float(direction * (exit_price / entry - 1) * 1e4)
    return {"entry_ts": entry_ts, "exit_ts": exit_ts, "gross_bps": gross_bps,
            "stop_hit": stop_hit, "gap_through_stop_bps": gap_bps}


def frozen_events() -> pd.DataFrame:
    universe = pd.read_csv(RESULTS / "universe_classifier.csv").set_index("symbol")
    primary = pd.read_csv(RESULTS / "events.csv")
    primary = primary[(primary.architecture == "vol_expansion") &
                      (primary.tier == "HV_A") & (primary.horizon_h == 24)].copy()
    primary["candidate"] = "primary_hv_a_volatility_expansion"
    primary["direction"] = np.sign(primary.zret)
    primary["signal_strength"] = primary.range_ratio
    secondary = pd.read_csv(RESULTS / "funding_events.csv")
    secondary = secondary[(secondary.architecture == "funding_crowding_continuation") &
                          (secondary.horizon_h == 24)].copy()
    secondary["candidate"] = "secondary_funding_crowding_continuation"
    secondary["direction"] = np.sign(secondary.extension)
    secondary["signal_strength"] = secondary.extension.abs()
    events = pd.concat([primary, secondary], ignore_index=True, sort=False)
    cost_at_10 = 12.0 + universe.spread_bps + universe.roundtrip_slippage_bps_10
    events["base_cost_bps"] = events.symbol.map(cost_at_10)
    events = events.dropna(subset=["base_cost_bps"])
    rows = []
    for row in events.itertuples():
        outcome = path_return(row.symbol, int(row.event_ts), float(row.direction))
        if outcome is not None:
            rows.append({"candidate": row.candidate, "symbol": row.symbol,
                         "signal_ts": int(row.event_ts), "direction": row.direction,
                         "signal_strength": float(row.signal_strength),
                         "base_cost_bps": float(row.base_cost_bps), **outcome})
    return pd.DataFrame(rows).sort_values(
        ["entry_ts", "candidate", "signal_strength", "symbol"],
        ascending=[True, True, False, True])


def portfolio(events: pd.DataFrame, cost_multiplier: float, spec: dict) -> dict:
    p = spec["portfolio"]
    equity = float(p["initial_equity_usdt"])
    fraction = float(p["position_notional_fraction_of_equity"])
    max_open = int(p["maximum_concurrent_positions"])
    open_positions: list[dict] = []
    curve = [(int(events.entry_ts.min()), equity)] if not events.empty else []
    accepted, skipped = [], 0

    def settle(until: int) -> None:
        nonlocal equity, open_positions
        due = sorted((x for x in open_positions if x["exit_ts"] <= until), key=lambda x: x["exit_ts"])
        for trade in due:
            equity += trade["notional"] * trade["net_bps"] / 1e4
            curve.append((trade["exit_ts"], equity))
            open_positions.remove(trade)

    for row in events.itertuples():
        settle(int(row.entry_ts))
        if len(open_positions) >= max_open or any(x["symbol"] == row.symbol for x in open_positions):
            skipped += 1
            continue
        notional = equity * fraction
        net_bps = float(row.gross_bps - row.base_cost_bps * cost_multiplier)
        trade = {"symbol": row.symbol, "exit_ts": int(row.exit_ts), "notional": notional,
                 "net_bps": net_bps, "gap_bps": row.gap_through_stop_bps}
        open_positions.append(trade)
        accepted.append(trade)
    settle(10**30)
    values = pd.Series([value for _, value in curve], dtype=float)
    drawdown = values / values.cummax() - 1 if not values.empty else pd.Series(dtype=float)
    realized_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
    conservative_drawdown = max(-1.0, realized_drawdown - float(p["maximum_gross_exposure_fraction"]) * .10)
    start_ts = int(events.entry_ts.min()) if not events.empty else 0
    end_ts = int(events.exit_ts.max()) if not events.empty else 0
    days = max(1.0, (end_ts - start_ts) / 86_400_000)
    daily_growth = (equity / p["initial_equity_usdt"]) ** (1 / days) - 1 if equity > 0 else -1.0
    return {
        "cost_multiplier": cost_multiplier, "accepted_trades": len(accepted),
        "skipped_capacity_or_symbol": skipped, "ending_equity_usdt": equity,
        "total_return_fraction": equity / p["initial_equity_usdt"] - 1,
        "geometric_daily_growth_fraction": daily_growth,
        "realized_exit_to_exit_drawdown_fraction": realized_drawdown,
        "conservative_drawdown_with_open_stop_risk_fraction": conservative_drawdown,
        "maximum_gap_through_stop_bps": max((x["gap_bps"] for x in accepted), default=0.0),
    }


def prospective_status(spec: dict) -> dict:
    journal = Path(__file__).with_name("prospective") / "observations.jsonl"
    rows = []
    if journal.exists():
        rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    closed = [row for row in rows if row.get("outcome_status") == "CLOSED"]
    return {
        "journal_exists": journal.exists(), "observations": len(rows),
        "closed_events": len(closed), "required_days": spec["shadow_ready_minimum"]["prospective_calendar_days"],
        "required_closed_each_candidate": spec["shadow_ready_minimum"]["prospective_closed_events_each_candidate"],
        "shadow_ready": False,
    }


def main() -> None:
    spec = json.loads(SPEC_PATH.read_text())
    events = frozen_events()
    candidates = {}
    for name, group in events.groupby("candidate"):
        candidates[name] = {
            "diagnostic_events": len(group), "symbols": group.symbol.nunique(),
            "stop_hit_rate": float(group.stop_hit.mean()),
            "gap_hit_rate": float((group.gap_through_stop_bps > 0).mean()),
            "max_gap_through_stop_bps": float(group.gap_through_stop_bps.max()),
            "portfolio_stress": [portfolio(group, multiple, spec) for multiple in spec["execution"]["cost_stress_multipliers"]],
        }
    result = {
        "spec_sha256": spec_hash(), "freeze_timestamp_utc": spec["freeze_timestamp_utc"],
        "classification": "DIAGNOSTIC_ONLY_CURRENT_SURVIVOR_UNIVERSE",
        "historical_lookahead_removed": False,
        "historical_lookahead_blocker": "Bitget current contract response lacks usable historical membership and delisted contracts",
        "candidates": candidates, "prospective": prospective_status(spec),
        "production_touched": False, "real_orders_sent": 0, "shadow_ready": False,
    }
    output = Path(__file__).with_name("VALIDATION_RESULTS.json")
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
