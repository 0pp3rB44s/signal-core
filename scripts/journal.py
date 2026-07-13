#!/usr/bin/env python3
"""Handmatig trade-journal (fase 1, spoor A) — PLAN_VOORUIT.md.

Zelfde eerlijke meetlat als de bot: elke trade vastleggen, expectancy in R.

    python3 scripts/journal.py add --symbol SOLUSDT --dir LONG \
        --entry 76.10 --stop 75.40 --exit 78.20 --setup "sweep 75.39 pool x1, TP pool 78.24"
    python3 scripts/journal.py list
    python3 scripts/journal.py stats
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "data_store" / "manual_journal.csv"
FIELDS = ["timestamp", "symbol", "direction", "entry", "stop", "exit", "r_multiple", "setup", "les"]
FEE_R_NOTE = 0.0  # exit is je echte fill; fees zitten al in je account-resultaat


def load() -> list[dict]:
    if not PATH.exists():
        return []
    with PATH.open() as f:
        return list(csv.DictReader(f))


def save_row(row: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not PATH.exists()
    with PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_add(a) -> None:
    direction = a.dir.upper()
    entry, stop, exit_ = float(a.entry), float(a.stop), float(a.exit)
    risk = abs(entry - stop)
    if risk <= 0:
        sys.exit("stop mag niet gelijk zijn aan entry")
    move = (exit_ - entry) if direction == "LONG" else (entry - exit_)
    r = round(move / risk, 3)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": a.symbol.upper(),
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "exit": exit_,
        "r_multiple": r,
        "setup": a.setup or "",
        "les": a.les or "",
    }
    save_row(row)
    print(f"genoteerd: {row['symbol']} {direction} -> {r:+.2f}R")
    cmd_stats(None)


def cmd_list(_a) -> None:
    rows = load()
    if not rows:
        print("journal is leeg")
        return
    for r in rows:
        print(f"{r['timestamp'][:16]}  {r['symbol']:9} {r['direction']:5} {float(r['r_multiple']):+6.2f}R  {r['setup'][:50]}")


def cmd_stats(_a) -> None:
    rows = load()
    n = len(rows)
    if n == 0:
        print("\nnog geen trades — stats volgen vanaf de eerste notering")
        return
    rs = [float(r["r_multiple"]) for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    wr = 100 * len(wins) / n
    exp = sum(rs) / n
    print(f"\n=== HAND-EXPECTANCY (n={n}) ===")
    print(f"  win rate : {wr:.0f}%")
    print(f"  avg win  : {sum(wins)/len(wins):+.2f}R" if wins else "  avg win  : -")
    print(f"  avg loss : {sum(losses)/len(losses):+.2f}R" if losses else "  avg loss : -")
    print(f"  expectancy: {exp:+.3f}R per trade | totaal {sum(rs):+.1f}R")
    # fase-2 beslisregel (PLAN_VOORUIT.md)
    if n < 20:
        print(f"  fase-2 oordeel: nog {20-n} trades te gaan voor een oordeel")
    elif exp > 0:
        print("  fase-2 oordeel: POSITIEF — dit is de edge; bot wordt copiloot")
    else:
        print("  fase-2 oordeel: negatief — eerlijk blijven: niet opschalen")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add")
    pa.add_argument("--symbol", required=True)
    pa.add_argument("--dir", required=True, choices=["LONG", "SHORT", "long", "short"])
    pa.add_argument("--entry", required=True)
    pa.add_argument("--stop", required=True)
    pa.add_argument("--exit", required=True)
    pa.add_argument("--setup", default="")
    pa.add_argument("--les", default="")
    pa.set_defaults(fn=cmd_add)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
