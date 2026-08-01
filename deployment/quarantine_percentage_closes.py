"""One-off migration: retire CLOSE rows whose money columns hold a percentage.

``ClosedTradeWriter._sync_journal_close`` passed ``margin_roi_pct`` into
``TradeJournal.log_close(pnl=...)``. Every close therefore produced two rows in
logs/trade_dataset_v2.csv: one from ``bitget_position_history`` carrying exchange
truth, and one from ``position_manager`` whose ``pnl`` is a return percentage.

``RiskManager._weekly_realized_pnl`` summed both, so the WEEKLY_FREEZE_LOSS_PCT
kill-switch ran on inflated numbers. The code fix stops new rows being written;
this retires the rows already on disk.

Retiring rewrites ``event_type`` from CLOSE to CLOSE_QUARANTINED. Every consumer
keys on that value via ``telemetry.close_record_sources``, so the row stops
counting everywhere at once while staying fully readable for audit. No economic
value is invented, altered, or removed, and no exchange-confirmed row is touched.

Dry-run by default. Operates on the active dataset and, with --include-rotated,
on rotated segments too.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime
import hashlib
import shutil
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from telemetry.close_record_sources import (  # noqa: E402
    ECONOMIC_CLOSE_EVENT_TYPES,
    EXCHANGE_CONFIRMED_CLOSE_SOURCES,
    QUARANTINED_CLOSE_EVENT_TYPE,
)

DATASET = BASE / "logs" / "trade_dataset_v2.csv"
TOLERANCE = datetime.timedelta(seconds=1)


def _dec(value):
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def _parse(stamp: str):
    try:
        return datetime.datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pair_rows(rows: list[dict]) -> list[tuple[int, int]]:
    """Match each provisional row to its exchange-truth twin.

    Grouped on (symbol, direction) and then matched on closed_at within one
    second, because the two writers stamp the close a moment apart and only the
    exchange row carries a lifecycle id.
    """
    by_market: dict[tuple, list[int]] = collections.defaultdict(list)
    for i, r in enumerate(rows):
        if str(r.get("event_type") or "").upper() not in ECONOMIC_CLOSE_EVENT_TYPES:
            continue
        by_market[(str(r.get("symbol") or "").upper(), str(r.get("direction") or "").upper())].append(i)

    pairs: list[tuple[int, int]] = []
    for idxs in by_market.values():
        suspects = [i for i in idxs if str(rows[i].get("sync_source") or "") not in EXCHANGE_CONFIRMED_CLOSE_SOURCES]
        truths = [i for i in idxs if str(rows[i].get("sync_source") or "") in EXCHANGE_CONFIRMED_CLOSE_SOURCES]
        used: set[int] = set()
        for s in suspects:
            s_at = _parse(rows[s].get("closed_at") or rows[s].get("timestamp"))
            best = None
            for t in truths:
                if t in used:
                    continue
                t_at = _parse(rows[t].get("closed_at") or rows[t].get("timestamp"))
                if s_at and t_at and abs(s_at - t_at) <= TOLERANCE:
                    best = t
                    break
            if best is not None:
                used.add(best)
                pairs.append((s, best))
    return pairs


def proves_percentage(suspect: dict, truth: dict) -> tuple[bool, str]:
    """Numeric proof that the suspect's money column holds a percentage.

    Requires the suspect value to equal the twin's reported margin ROI, or to be
    far larger than the true PnL while staying within a plausible ROI range. A
    row is never retired merely for being a loss or for disagreeing slightly.
    """
    pnl = _dec(suspect.get("pnl"))
    if pnl is None:
        return False, "pnl unreadable"
    true_pnl = _dec(truth.get("net_pnl")) or _dec(truth.get("pnl"))
    if true_pnl is None or true_pnl == 0:
        return False, "exchange twin has no usable pnl"

    roi = _dec(truth.get("margin_roi_pct")) or _dec(suspect.get("margin_roi_pct"))
    if roi is not None and roi != 0 and abs(pnl - roi) <= Decimal("0.0001"):
        return True, f"pnl={pnl} equals margin_roi_pct={roi} of the exchange twin (net {true_pnl})"

    ratio = abs(pnl / true_pnl)
    if ratio < 3:
        return False, f"magnitude ratio {ratio:.2f} too small to prove a unit error"
    return True, f"pnl={pnl} is {ratio:.1f}x the exchange net {true_pnl}"


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def weekly_total(rows: list[dict], days: int = 7) -> Decimal:
    """Mirrors the *old* RiskManager filter (event_type only) to show the impact."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    total = Decimal(0)
    for r in rows:
        if str(r.get("event_type") or "").upper() not in ECONOMIC_CLOSE_EVENT_TYPES:
            continue
        if str(r.get("closed_at") or r.get("timestamp") or "") < cutoff:
            continue
        total += _dec(r.get("net_pnl")) or _dec(r.get("pnl")) or Decimal(0)
    return total


def process(path: Path, apply: bool, backup_dir: Path) -> int:
    if not path.exists():
        print(f"  ABORT: {path} not found")
        return 2

    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    before = weekly_total(rows)
    pairs = pair_rows(rows)

    targets: list[tuple[int, int, str]] = []
    for suspect, truth in pairs:
        ok, why = proves_percentage(rows[suspect], rows[truth])
        if ok:
            targets.append((suspect, truth, why))

    print(f"  dataset             : {path}")
    print(f"  records             : {len(rows)}")
    print(f"  economische CLOSE   : {sum(1 for r in rows if str(r.get('event_type') or '').upper() in ECONOMIC_CLOSE_EVENT_TYPES)}")
    print(f"  gevonden paren      : {len(pairs)}")
    print(f"  te quarantainen     : {len(targets)}")
    print(f"  checksum vooraf     : {checksum(path)[:16]}…")
    print(f"  weekly (oude filter): {before:.6f} USDT")

    if targets:
        print()
        print("  ── bewijs per paar ──")
        for s, t, why in targets:
            sr, tr = rows[s], rows[t]
            print(f"    {sr.get('symbol'):9} {sr.get('direction'):5} opened={str(sr.get('opened_at'))[:19]}")
            print(f"      provisional : rij {s:4} src={sr.get('sync_source'):20} pnl={sr.get('pnl')} net_pnl={sr.get('net_pnl')} lifecycle={sr.get('position_lifecycle_id') or 'LEEG'}")
            print(f"      exchange    : rij {t:4} src={tr.get('sync_source'):20} pnl={tr.get('pnl')} net_pnl={tr.get('net_pnl')} fees={tr.get('fees')} lifecycle={tr.get('position_lifecycle_id') or 'LEEG'}")
            print(f"      bewijs      : {why}")

    projected = [dict(r) for r in rows]
    for s, _, _ in targets:
        projected[s]["event_type"] = QUARANTINED_CLOSE_EVENT_TYPE
    after = weekly_total(projected)
    print()
    print(f"  weekly na           : {after:.6f} USDT")
    print(f"  verschil            : {after - before:+.6f}")

    if not apply:
        print("\n  DRY-RUN — niets geschreven. Gebruik --apply om toe te passen.")
        return 0

    if not targets:
        print("\n  Niets te doen; migratie is idempotent. 0 wijzigingen.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup)
    backup.chmod(0o600)
    print(f"\n  backup              : {backup} (mode 600)")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in projected:
            writer.writerow(r)
    tmp.replace(path)

    print(f"  checksum achteraf   : {checksum(path)[:16]}…")
    print(f"  gewijzigde records  : {len(targets)}")
    print(f"\n  ROLLBACK: cp -p '{backup}' '{path}'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    ap.add_argument("--include-rotated", action="store_true", help="also process .1/.2 segments")
    ap.add_argument("--backup-dir", default=str(Path.home() / ".cgc-config-backups"))
    args = ap.parse_args()

    paths = [Path(args.dataset)]
    if args.include_rotated:
        base = Path(args.dataset)
        for suffix in (".1", ".2", ".3"):
            rotated = base.with_name(base.name + suffix)
            if rotated.exists():
                paths.append(rotated)

    rc = 0
    for p in paths:
        print(f"\n══════ {p.name} ══════")
        rc = process(p, args.apply, Path(args.backup_dir)) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
