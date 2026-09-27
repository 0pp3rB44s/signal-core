#!/usr/bin/env python3
"""Read-only post-hoc labeller for MicroFlow near-miss episodes.

Answers, for episodes that never became trades: *would it have worked?* Labels are
computed from market data recorded **after** the decision, which makes them useful
for research and disqualifying for anything live.

Two hard separations keep that honest:

* This script imports nothing from `execution/` or `app/runner`, holds no exchange
  credentials for trading, and writes only to its own output file.
* Every emitted row carries ``research_only: true`` and ``uses_future_data: true``.
  A label that leaked into an entry decision would be look-ahead bias wearing the
  costume of a feature, so the marker travels with the data.

Usage:
    python scripts/label_near_miss.py --data-dir data_store/microflow_live \\
        [--out labels.jsonl] [--fee-bps 12] [--tp-bps 40] [--sl-bps 20] [--hold-ms 600000]
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from pathlib import Path

RESEARCH_MARKER = {"research_only": True, "uses_future_data": True}


def load_episodes(data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(str(data_dir / "near_miss" / "segments" / "*.jsonl.gz"))):
        try:
            with gzip.open(path, "rt") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, EOFError, json.JSONDecodeError) as exc:
            print(f"skip {os.path.basename(path)}: {exc}", file=sys.stderr)
    return rows


def load_prices(data_dir: Path) -> dict[str, list[tuple[int, float]]]:
    """Trade prints per symbol, oldest first, from the collector's own segments."""
    prices: dict[str, list[tuple[int, float]]] = {}
    for path in sorted(glob.glob(str(data_dir / "trades" / "segments" / "*.jsonl.gz"))):
        try:
            with gzip.open(path, "rt") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    symbol, ts, price = row.get("symbol"), row.get("timestamp_local"), row.get("price")
                    if symbol and isinstance(ts, (int, float)) and isinstance(price, (int, float)):
                        prices.setdefault(symbol, []).append((int(ts), float(price)))
        except (OSError, EOFError, json.JSONDecodeError):
            continue
    for series in prices.values():
        series.sort()
    return prices


def label(episode: dict, series: list[tuple[int, float]], *,
          tp_bps: float, sl_bps: float, hold_ms: int, fee_bps: float) -> dict | None:
    """Walk forward from the decision and record which barrier was touched first."""
    ref = (episode.get("values") or {}).get("mid_price")
    start = episode.get("timestamp_ms")
    side = episode.get("side")
    if not (isinstance(ref, (int, float)) and ref > 0 and isinstance(start, int) and side in ("LONG", "SHORT")):
        return None
    sign = 1.0 if side == "LONG" else -1.0
    mfe = mae = 0.0
    outcome, t_hit = "NEITHER", None
    for ts, price in series:
        if ts < start:
            continue
        if ts > start + hold_ms:
            break
        move = (price - ref) / ref * 10_000.0 * sign
        mfe, mae = max(mfe, move), min(mae, move)
        if move >= tp_bps:
            outcome, t_hit = "TP_FIRST", ts - start
            break
        if move <= -sl_bps:
            outcome, t_hit = "SL_FIRST", ts - start
            break
    gross = {"TP_FIRST": tp_bps, "SL_FIRST": -sl_bps}.get(outcome, 0.0)
    return {
        **RESEARCH_MARKER,
        "episode_id": episode.get("episode_id"),
        "symbol": episode.get("symbol"), "side": side, "state": episode.get("state"),
        "reason": episode.get("reason"), "last_failed_gate": episode.get("last_failed_gate"),
        "reference_price": ref, "decision_ts_ms": start,
        "mfe_bps": round(mfe, 4), "mae_bps": round(mae, 4),
        "outcome": outcome, "time_to_barrier_ms": t_hit,
        "modeled_gross_bps": round(gross, 4),
        "fee_bps": fee_bps,
        "modeled_net_bps": round(gross - fee_bps, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data_store/microflow_live")
    ap.add_argument("--out", default="near_miss_labels.jsonl")
    ap.add_argument("--tp-bps", type=float, default=40.0)
    ap.add_argument("--sl-bps", type=float, default=20.0)
    ap.add_argument("--hold-ms", type=int, default=600_000)
    ap.add_argument("--fee-bps", type=float, default=12.0)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    episodes = load_episodes(data_dir)
    prices = load_prices(data_dir)
    print(f"episodes={len(episodes)} symbols_with_prices={len(prices)}", file=sys.stderr)

    # Label the decision points only: an episode's outcome is defined at the moment
    # it stopped being actionable, not at every heartbeat in between.
    decisions = [e for e in episodes if e.get("state") in ("CANDIDATE", "BLOCKED", "EXPIRED")]
    written = 0
    with open(args.out, "w") as out:
        for episode in decisions:
            row = label(episode, prices.get(episode.get("symbol"), []),
                        tp_bps=args.tp_bps, sl_bps=args.sl_bps,
                        hold_ms=args.hold_ms, fee_bps=args.fee_bps)
            if row:
                out.write(json.dumps(row) + "\n")
                written += 1
    print(f"labelled={written} -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
