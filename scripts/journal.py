#!/usr/bin/env python3
"""Handmatig trade-journal met finetune-lus (fase 1, spoor A).

Zelfde eerlijke meetlat als de bot, plus de kwaliteitsvelden uit
docs/ENTRY_PLAYBOOK.md zodat stats de expectancy per ingredient splitst.

    python3 scripts/journal.py add --symbol SOLUSDT --dir LONG \
        --entry 76.10 --stop 75.35 --exit 78.20 \
        --pool-tf 4H --touches 5 --sweep wick --bevestiging ja \
        --rr-plan 2.8 --sessie londen \
        --setup "sweep 75.39 x1 onderin range, TP pool 78.24 x5"
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
FIELDS = [
    "timestamp", "symbol", "direction", "entry", "stop", "exit", "r_multiple",
    "pool_tf", "touches", "sweep", "bevestiging", "rr_plan", "sessie",
    "setup", "les",
]
MIN_BUCKET = 5  # minimaal aantal trades per kant voor een uitsplitsing


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
        "symbol": a.symbol.upper(), "direction": direction,
        "entry": entry, "stop": stop, "exit": exit_, "r_multiple": r,
        "pool_tf": (a.pool_tf or "").upper(), "touches": a.touches or "",
        "sweep": (a.sweep or "").lower(), "bevestiging": (a.bevestiging or "").lower(),
        "rr_plan": a.rr_plan or "", "sessie": (a.sessie or "").lower(),
        "setup": a.setup or "", "les": a.les or "",
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
        print(
            f"{r['timestamp'][:16]}  {r['symbol']:9} {r['direction']:5} "
            f"{float(r['r_multiple']):+6.2f}R  [{r.get('pool_tf','')}/x{r.get('touches','')}/"
            f"{r.get('sweep','')}/bev={r.get('bevestiging','')}]  {r.get('setup','')[:40]}"
        )


def _exp(rs: list[float]) -> str:
    if not rs:
        return "-"
    w = sum(1 for r in rs if r > 0)
    return f"n={len(rs):2d} WR={100*w/len(rs):3.0f}% exp={sum(rs)/len(rs):+.2f}R"


def _split(rows: list[dict], veld: str, laag) -> None:
    groepen: dict[str, list[float]] = {}
    for r in rows:
        key = laag(r)
        if key:
            groepen.setdefault(key, []).append(float(r["r_multiple"]))
    zichtbaar = {k: v for k, v in groepen.items() if len(v) >= MIN_BUCKET}
    if len(zichtbaar) >= 2:
        print(f"  per {veld}:")
        for k, v in sorted(zichtbaar.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            print(f"    {k:12} {_exp(v)}")


def cmd_stats(_a) -> None:
    rows = load()
    n = len(rows)
    if n == 0:
        print("\nnog geen trades — stats volgen vanaf de eerste notering")
        return
    rs = [float(r["r_multiple"]) for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    print(f"\n=== HAND-EXPECTANCY (n={n}) ===")
    print(f"  win rate : {100*len(wins)/n:.0f}%")
    print(f"  avg win  : {sum(wins)/len(wins):+.2f}R" if wins else "  avg win  : -")
    print(f"  avg loss : {sum(losses)/len(losses):+.2f}R" if losses else "  avg loss : -")
    print(f"  expectancy: {sum(rs)/n:+.3f}R per trade | totaal {sum(rs):+.1f}R")
    if n < 20:
        print(f"  fase-2 oordeel: nog {20-n} trades te gaan voor een oordeel")
    elif sum(rs) / n > 0:
        print("  fase-2 oordeel: POSITIEF — dit is de edge; bot wordt copiloot")
    else:
        print("  fase-2 oordeel: negatief — eerlijk blijven: niet opschalen")
    # finetune-lus: expectancy per ingredient (ENTRY_PLAYBOOK.md)
    print("\n=== FINETUNE-LUS (welk ingredient wint?) ===")
    _split(rows, "pool-TF", lambda r: r.get("pool_tf") or None)
    _split(rows, "pool-sterkte", lambda r: ("x3+" if int(r["touches"]) >= 3 else "x1-2") if str(r.get("touches") or "").isdigit() else None)
    _split(rows, "sweep-karakter", lambda r: r.get("sweep") or None)
    _split(rows, "bevestiging", lambda r: r.get("bevestiging") or None)
    _split(rows, "sessie", lambda r: r.get("sessie") or None)
    print(f"  (uitsplitsing verschijnt zodra een categorie {MIN_BUCKET}+ trades per kant heeft)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add")
    pa.add_argument("--symbol", required=True)
    pa.add_argument("--dir", required=True, choices=["LONG", "SHORT", "long", "short"])
    pa.add_argument("--entry", required=True)
    pa.add_argument("--stop", required=True)
    pa.add_argument("--exit", required=True)
    pa.add_argument("--pool-tf", default="", help="1H of 4H (playbook laag 1)")
    pa.add_argument("--touches", default="", help="aantal equal highs/lows van de geveegde pool")
    pa.add_argument("--sweep", default="", help="wick of grind (laag 2)")
    pa.add_argument("--bevestiging", default="", help="ja/nee structuurbreuk afgewacht (laag 3)")
    pa.add_argument("--rr-plan", default="", help="geplande RR naar de doelpool (laag 4)")
    pa.add_argument("--sessie", default="", help="azie/londen/ny/nacht (laag 5)")
    pa.add_argument("--setup", default="")
    pa.add_argument("--les", default="")
    pa.set_defaults(fn=cmd_add)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
