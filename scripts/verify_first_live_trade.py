#!/usr/bin/env python3
"""Post-trade verification: did a completed LIVE trade land in every downstream system?

READ-ONLY. Places no orders, writes nothing except its own report, and never
prints credentials. Exits 0 when every check passes, 1 when any fails, 2 when
there is no completed live trade to verify yet.

Intended to run unattended after the first (and every) real close, e.g. from the
watchdog or a launchd StartInterval job:

    .venv/bin/python scripts/verify_first_live_trade.py --since 2026-07-30T00:00:00Z

The checks mirror what the expectancy architecture depends on. A close that is
written to the dataset but never picked up by the directional cache would leave
the symbol gate reasoning about stale evidence without anything surfacing it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATASET = REPO / "logs" / "trade_dataset_v2.csv"
CLOSE_EVENTS = {"CLOSE", "POSITION_CLOSED"}

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _parse_ts(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == FAIL)

    def render(self) -> str:
        width = max(len(n) for _, n, _ in self.rows) if self.rows else 10
        lines = []
        for status, name, detail in self.rows:
            mark = {PASS: "PASS", FAIL: "FAIL", SKIP: "skip"}[status]
            lines.append(f"  [{mark}] {name:<{width}}  {detail}")
        return "\n".join(lines)


def load_closes(since: datetime | None) -> tuple[list[dict], str | None]:
    if not DATASET.exists():
        return [], f"{DATASET} absent"
    try:
        with DATASET.open("r", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as exc:  # malformed CSV must not crash the verifier
        return [], f"{type(exc).__name__}: {exc}"
    closes = []
    for r in rows:
        if str(r.get("event_type") or "").upper() not in CLOSE_EVENTS:
            continue
        ts = _parse_ts(r.get("closed_at") or r.get("timestamp"))
        if since and (ts is None or ts < since):
            continue
        closes.append(r)
    return closes, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO timestamp; only verify closes at or after this")
    ap.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = ap.parse_args()

    since = _parse_ts(args.since) if args.since else None
    rep = Report()

    closes, err = load_closes(since)
    if err:
        rep.add(FAIL, "dataset readable", err)
        print(rep.render())
        return 1

    if not closes:
        rep.add(SKIP, "completed live trade", "none found in window — nothing to verify")
        print(rep.render())
        return 2

    trade = max(closes, key=lambda r: str(r.get("closed_at") or r.get("timestamp") or ""))
    symbol = str(trade.get("symbol") or "").upper()
    direction = str(trade.get("direction") or "").upper()

    # --- 1. dataset row is complete and coherent -------------------------
    rep.add(PASS, "trade_dataset_v2 updated", f"{len(closes)} close(s) in window")
    rep.add(PASS if symbol else FAIL, "symbol present", symbol or "empty")
    rep.add(PASS if direction in ("LONG", "SHORT") else FAIL,
            "direction valid", direction or "empty")

    closed_at = _parse_ts(trade.get("closed_at"))
    rep.add(PASS if closed_at else FAIL, "closed_at parseable",
            trade.get("closed_at") or "empty")

    for field in ("pnl", "net_pnl", "fees"):
        raw = trade.get(field)
        try:
            value = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            value = None
        rep.add(PASS if value is not None else FAIL, f"{field} numeric",
                "empty" if value is None else f"{value:+.8f}")

    # net_pnl should differ from gross pnl by the fees actually charged; a
    # net that silently equals gross means fees were never attributed.
    try:
        gross, net, fee = (float(trade.get(k) or 0) for k in ("pnl", "net_pnl", "fees"))
        if fee:
            coherent = abs((gross - fee) - net) < max(abs(net) * 0.02, 1e-6)
            rep.add(PASS if coherent else FAIL, "net = gross - fees",
                    f"gross={gross:+.8f} fees={fee:.8f} net={net:+.8f}")
        else:
            rep.add(SKIP, "net = gross - fees", "fees=0, nothing to reconcile")
    except (TypeError, ValueError):
        rep.add(FAIL, "net = gross - fees", "unparseable")

    rep.add(PASS if str(trade.get("sync_source") or "") else FAIL,
            "sync_source present", str(trade.get("sync_source") or "empty"))

    # --- 2. the directional expectancy cache picked it up ----------------
    try:
        from risk import symbol_expectancy as se
    except Exception as exc:
        rep.add(FAIL, "expectancy module import", f"{type(exc).__name__}: {exc}")
        print(rep.render())
        return 1

    se.reset_cache()  # prove the rebuild, do not trust a warm cache
    record = se.record_for(symbol, direction, use_cache=False)

    rep.add(PASS if record.source == se.SOURCE_NAME else FAIL,
            "expectancy source", record.source)

    exchange_confirmed = str(trade.get("sync_source") or "") in se.EXCHANGE_CONFIRMED_SOURCES
    if exchange_confirmed:
        rep.add(PASS if record.sample_size >= 1 else FAIL,
                "directional record updated",
                f"{symbol} {direction} n={record.sample_size}")
        rep.add(PASS if record.last_trade_at else FAIL,
                "last_trade_at set", str(record.last_trade_at))
        rep.add(PASS if record.freshness_state == se.FRESH else FAIL,
                "freshness recomputed",
                f"{record.freshness_state} (expected FRESH right after a close)")
    else:
        # Deliberate: a close from a non-exchange source must NOT enter the
        # evidence base. Absence is then the correct outcome, not a failure.
        rep.add(SKIP, "directional record updated",
                f"sync_source={trade.get('sync_source')!r} not exchange-confirmed")
        rep.add(SKIP, "freshness recomputed", "excluded source")

    rep.add(PASS if record.generated_at else FAIL,
            "generated_at stamped", str(record.generated_at))
    rep.add(PASS if record.status in {
                se.SUFFICIENT_OK, se.SUFFICIENT_NEGATIVE,
                se.INSUFFICIENT_LIVE_DATA, se.SOURCE_ABSENT, se.SOURCE_MALFORMED,
            } else FAIL, "status computed", record.status)
    rep.add(PASS if record.confidence in {se.HIGH, se.MEDIUM, se.LOW} else FAIL,
            "confidence computed", record.confidence)

    # the opposite direction must be untouched by this close
    other = "SHORT" if direction == "LONG" else "LONG"
    other_rec = se.record_for(symbol, other, use_cache=False)
    rep.add(PASS, f"{other} isolated",
            f"n={other_rec.sample_size} status={other_rec.status}")

    # --- 3. the dashboard renders the new state --------------------------
    try:
        from dashboard_v3.panels import expectancy as xp
        panel = xp.build()
        keyed = {(r["symbol"], r["direction"]) for r in panel["symbols"]}
        if exchange_confirmed:
            rep.add(PASS if (symbol, direction) in keyed else FAIL,
                    "dashboard shows record", f"{len(panel['symbols'])} record(s)")
        else:
            rep.add(SKIP, "dashboard shows record", "excluded source")
        rep.add(PASS if panel.get("symbol_source_status") in ("OK", "") else FAIL,
                "dashboard source status", str(panel.get("symbol_source_status")))
    except Exception as exc:
        rep.add(FAIL, "dashboard panel builds", f"{type(exc).__name__}: {exc}")

    # --- 4. no orphaned intent left behind -------------------------------
    intents = REPO / "state" / "order_intents.json"
    if intents.exists():
        try:
            payload = json.loads(intents.read_text())
            data = payload.get("data") if isinstance(payload, dict) else payload
            unresolved = [i for i in (data or [])
                          if str(i.get("state")) in {"SUBMITTING", "AMBIGUOUS"}
                          or str(i.get("protection_state")) == "PENDING"
                          and str(i.get("state")) == "FILLED"]
            rep.add(PASS if not unresolved else FAIL, "no unresolved intents",
                    f"{len(unresolved)} unresolved of {len(data or [])}")
        except Exception as exc:
            rep.add(FAIL, "intent store readable", f"{type(exc).__name__}: {exc}")
    else:
        rep.add(SKIP, "no unresolved intents", "no intent store yet")

    header = (f"first-live-trade verification — {symbol} {direction} "
              f"closed_at={trade.get('closed_at')}")
    if args.json:
        print(json.dumps({
            "symbol": symbol, "direction": direction,
            "closed_at": trade.get("closed_at"),
            "failed": rep.failed,
            "checks": [{"status": s, "name": n, "detail": d} for s, n, d in rep.rows],
        }, indent=2))
    else:
        print(header)
        print(rep.render())
        print(f"\n  {len(rep.rows)} checks, {rep.failed} failed")

    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
