"""Funding-extreme crowding pilot using public Bitget history only."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path

import numpy as np
import pandas as pd

from run_hv_pilot import PublicClient, frame, nonoverlapping, robust_stats


def funding_history(client: PublicClient, symbol: str, pages: int = 6) -> list[dict]:
    target = client.cache / f"{symbol}_funding_{pages}p.json"
    if target.exists():
        return json.loads(target.read_text())
    rows = []
    for page in range(1, pages + 1):
        batch = client.get("/api/v2/mix/market/history-fund-rate", {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "pageSize": "100", "pageNo": str(page),
        })
        if not batch:
            break
        rows.extend(batch)
    rows = list({int(row["fundingTime"]): row for row in rows}.values())
    target.write_text(json.dumps(rows, separators=(",", ":")))
    return rows


def crowding_events(symbol: str, candles: pd.DataFrame, funding: list[dict]) -> list[dict]:
    if not funding or candles.empty:
        return []
    c = candles.set_index("ts").sort_index()
    f = pd.DataFrame(funding)
    f["event_ts"] = pd.to_numeric(f.fundingTime)
    f["funding"] = pd.to_numeric(f.fundingRate)
    f = f.sort_values("event_ts").drop_duplicates("event_ts").reset_index(drop=True)
    # The just-published funding observation is known at event time. Rank it
    # against the trailing window including itself; no future observation enters.
    f["funding_pct"] = f.funding.rolling(90, min_periods=30).rank(pct=True)
    # Candle timestamps denote interval opens. Using close at the same timestamp
    # would leak the following hour; exact event-time opens are executable proxies.
    open_at = c.open.reindex(f.event_ts, method="ffill").to_numpy()
    f["entry_price"] = open_at
    f["ret24"] = f.entry_price / f.entry_price.shift(3) - 1  # three 8h funding intervals
    f["ret24_vol"] = f.ret24.rolling(30, min_periods=20).std().shift(1)
    f["extension"] = f.ret24 / f.ret24_vol
    extreme = ((f.funding_pct >= .95) & (f.extension >= 1.5)) | ((f.funding_pct <= .05) & (f.extension <= -1.5))
    out = []
    for idx in f.index[extreme]:
        trend = np.sign(f.at[idx, "extension"])
        event_ts = int(f.at[idx, "event_ts"])
        entry = float(f.at[idx, "entry_price"])
        for horizon in (4, 12, 24):
            future_ts = event_ts + horizon * 3_600_000
            if future_ts > c.index.max():
                continue
            future = c.open.reindex([future_ts], method="ffill").iloc[0]
            if pd.isna(future) or entry <= 0:
                continue
            raw = float(future / entry - 1)
            for architecture, direction in (
                ("funding_crowding_reversal", -trend),
                ("funding_crowding_continuation", trend),
            ):
                base_row = {
                    "architecture": architecture, "symbol": symbol,
                    "event_ts": event_ts, "horizon_h": horizon,
                    "signed_return_bps": direction * raw * 1e4,
                    "absolute_forward_move_bps": abs(raw) * 1e4,
                    "funding_rate": float(f.at[idx, "funding"]),
                    "funding_pct": float(f.at[idx, "funding_pct"]),
                    "extension": float(f.at[idx, "extension"]),
                    "stop_hit": False,
                }
                out.append(base_row)
                # One predeclared catastrophic stop, evaluated on the hourly
                # high/low path. A gap through the stop is not modelled, so this
                # remains suggestive rather than fill-proof.
                path = c.loc[(c.index >= event_ts) & (c.index < future_ts)]
                if direction > 0:
                    adverse = path.low / entry - 1
                else:
                    adverse = 1 - path.high / entry
                stopped = bool((adverse <= -.10).any())
                stop_row = dict(base_row)
                stop_row["architecture"] = architecture + "_stop10pct"
                stop_row["stop_hit"] = stopped
                if stopped:
                    stop_row["signed_return_bps"] = -1000.0
                    stop_row["absolute_forward_move_bps"] = 1000.0
                out.append(stop_row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("research/data/speculative_v1"))
    parser.add_argument("--input", type=Path, default=Path("research/results/speculative_v1"))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    client = PublicClient(args.cache)
    universe = pd.read_csv(args.input / "universe_classifier.csv")
    universe = universe[(universe.tier == "HV_A") & universe.cost_bps_100.notna()]
    info = universe.set_index("symbol")

    def acquire(symbol: str):
        try:
            funding = funding_history(client, symbol)
            candle_path = args.cache / f"{symbol}_1H_180d.json"
            candles = frame(json.loads(candle_path.read_text()))
            return crowding_events(symbol, candles, funding), None
        except Exception as exc:
            return [], f"{symbol}: {exc}"

    rows, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for events, error in pool.map(acquire, info.index.tolist()):
            rows.extend(events)
            if error:
                errors.append(error)
    events = pd.DataFrame(rows)
    summaries = []
    if not events.empty:
        events["tier"] = "HV_A"
        events["estimated_cost_bps_100"] = events.symbol.map(info.cost_bps_100)
        for (architecture, horizon), group in events.groupby(["architecture", "horizon_h"]):
            group = nonoverlapping(group, int(horizon))
            cost = float(group.estimated_cost_bps_100.median())
            summaries.append({"architecture": architecture, "tier": "HV_A", "horizon_h": horizon,
                              "median_estimated_cost_bps_100": cost, **robust_stats(group, cost)})
    summary = pd.DataFrame(summaries).sort_values("net_edge_bps", ascending=False) if summaries else pd.DataFrame()
    events.to_csv(args.input / "funding_events.csv", index=False)
    summary.to_csv(args.input / "funding_summary.csv", index=False)
    print(json.dumps({"symbols": len(info), "events": len(events), "errors": errors,
                      "public_data_only": True, "orders_allowed": False}, indent=2))
    if not summary.empty:
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
