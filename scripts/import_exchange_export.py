#!/usr/bin/env python3
"""Importeer een Bitget position-history export (app: Orders -> Export) in de
leerdataset (logs/trade_dataset_v2.csv).

Waarom: sommige close-paden schreven historisch geen dataset-rij (zie
PATCH-067/ENA-case en de close-pad-fix op de aparte branch), waardoor de
leerloop trades miste. Een eigenaar-export is exchange truth en vult die gaten.

Werkwijze:
- Export-tijden zijn LOKAAL (CEST, UTC+2) -> genormaliseerd naar UTC.
- 'Position Pnl' = netto (na fees); geverifieerd tegen Bitget netProfit.
- Alleen BOT-trades worden geimporteerd: een rij moet matchen met een record
  in state/executed_trades.json (symbol + richting + opening binnen 3 min),
  daar komt ook de strategie-attributie vandaan. Handmatige trades (bijv. de
  SOL-trades van begin juli) horen niet in de leerdata en worden geskipt.
- Dedupe: bestaat er al een dataset-CLOSE-rij (symbol + richting + opening
  binnen 3 min), dan wordt de export-rij overgeslagen. Idempotent: het script
  twee keer draaien voegt niets dubbel toe.

Gebruik:
    python3 scripts/import_exchange_export.py <export.csv> [--apply]
Zonder --apply: dry-run (toont wat er zou gebeuren).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPORT_TZ_OFFSET_HOURS = 2  # Bitget-app exporteert in lokale tijd (CEST)
MATCH_WINDOW_SECONDS = 180
DATASET = "logs/trade_dataset_v2.csv"
STATE = "state/executed_trades.json"


def _num(s) -> float:
    s = str(s or "").replace("USDT", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_local(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") - timedelta(hours=EXPORT_TZ_OFFSET_HOURS)


def _load_export(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            m = re.match(r"([A-Z0-9]+)\s+(Long|Short)", str(row.get("Futures") or ""))
            if not m:
                continue
            size_m = re.match(r"([0-9.]+)", str(row.get("Closed amount") or ""))
            out.append({
                "symbol": m.group(1),
                "direction": m.group(2).upper(),
                "opened": _parse_local(row["Opening time"]),
                "closed": _parse_local(row["Closed time"]),
                "entry": _num(row["Average entry price"]),
                "exit": _num(row["Average closing price"]),
                "size": float(size_m.group(1)) if size_m else 0.0,
                "net": _num(row["Position Pnl"]),
                "gross": _num(row["Realized PnL"]),
                "fees": abs(_num(row["Opening fee"])) + abs(_num(row["Closing fee"])),
            })
    return out


def _nearest(target: dict, pool: list[dict]) -> dict | None:
    best, best_diff = None, None
    for item in pool:
        if item["symbol"] != target["symbol"] or item["direction"] != target["direction"]:
            continue
        diff = abs((item["opened"] - target["opened"]).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best = diff, item
    if best is not None and best_diff is not None and best_diff <= MATCH_WINDOW_SECONDS:
        return best
    return None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args:
        print(__doc__)
        return 2
    export_path = args[0]

    export = _load_export(export_path)

    state_pool = []
    for x in json.load(open(STATE))["data"]:
        try:
            opened = datetime.fromisoformat(str(x.get("opened_at"))).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        state_pool.append({
            "symbol": x.get("symbol"), "direction": str(x.get("direction") or "").upper(),
            "opened": opened, "strategy": x.get("strategy") or "unknown",
        })

    dataset_pool = []
    try:
        for r in csv.DictReader(open(DATASET)):
            if str(r.get("event_type", "")).upper() != "CLOSE":
                continue
            try:
                opened = datetime.fromisoformat(str(r.get("opened_at")).replace("Z", "+00:00")).replace(tzinfo=None)
            except (TypeError, ValueError):
                continue
            dataset_pool.append({"symbol": r.get("symbol"), "direction": r.get("direction"), "opened": opened})
    except FileNotFoundError:
        pass

    imported, skipped_manual, skipped_present = [], [], 0
    for t in export:
        bot = _nearest(t, state_pool)
        if bot is None:
            skipped_manual.append(t)
            continue
        if _nearest(t, dataset_pool) is not None:
            skipped_present += 1
            continue
        t["strategy"] = bot["strategy"]
        imported.append(t)

    print(f"export-rijen: {len(export)} | al in dataset: {skipped_present} | "
          f"manual/niet-bot geskipt: {len(skipped_manual)} | te importeren: {len(imported)}")
    for t in imported:
        print(f"  {t['symbol']:9} {t['direction']:5} {t['strategy'][:22]:22} "
              f"open={t['opened']:%m-%d %H:%M} net={t['net']:+.4f}")
    for t in skipped_manual:
        print(f"  SKIP manual: {t['symbol']} {t['direction']} open={t['opened']:%m-%d %H:%M} net={t['net']:+.4f}")

    if not apply:
        print("\nDry-run. Draai met --apply om te schrijven.")
        return 0

    from telemetry.trade_logger import append_closed_trade_row
    for t in imported:
        move_pct = ((t["exit"] - t["entry"]) / t["entry"] * 100.0) if t["entry"] else 0.0
        if t["direction"] == "SHORT":
            move_pct = -move_pct
        append_closed_trade_row(
            symbol=t["symbol"], strategy=t["strategy"], direction=t["direction"],
            entry_price=t["entry"], exit_price=t["exit"], size=t["size"],
            pnl=t["net"], pnl_pct=round(move_pct, 4),
            close_reason="exchange_export_backfill",
            opened_at=t["opened"].strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            closed_at=t["closed"].strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            extra={
                "entry": t["entry"], "exit": t["exit"],
                "net_pnl": t["net"], "exchange_truth_pnl": t["net"],
                "exchange_truth_fee": t["fees"], "fees": t["fees"],
                "result": "win" if t["net"] > 0 else "loss",
                "data_confidence": "EXCHANGE_TRUTH",
                "close_source": "bitget_export_backfill",
            },
        )
    print(f"\n{len(imported)} rijen geschreven naar {DATASET}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
