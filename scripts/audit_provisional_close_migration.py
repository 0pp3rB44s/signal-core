#!/usr/bin/env python3
"""Audit provisional closes against GET-only Bitget position history.

Default mode is read-only. ``--apply`` only appends locally reconciled economic
rows; it never sends, cancels, or modifies an exchange order.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal

from app.config import get_settings
from clients.bitget_rest import BitgetRestClient
from execution.close_dedup import economic_close_exists, segment_paths
from execution.close_reconciler import (
    AmbiguousLifecycle,
    CloseReconciliationUnavailable,
    economics_from_history,
    match_lifecycle,
)
from execution.closed_lifecycle_recorder import _opened_at_ms, load_provisional_rows
from telemetry.trade_logger import append_exchange_truth_close
from telemetry.close_record_sources import is_economic_close


def _history(client: BitgetRestClient) -> list[dict]:
    payload = client.get_position_history(limit=100)
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("list") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError("Bitget position-history returned no valid list")
    return rows


def audit(*, dataset: str, history: list[dict], apply: bool = False) -> dict:
    rows = load_provisional_rows(dataset)
    summary = {"safe": 0, "ambiguous_or_missing": 0, "existing": 0, "applied": 0}
    before = Decimal("0")
    for path in segment_paths(dataset):
        with path.open("r", newline="", encoding="utf-8", errors="replace") as handle:
            for existing in csv.DictReader(handle):
                if is_economic_close(existing) and existing.get("net_pnl") not in (None, ""):
                    before += Decimal(str(existing["net_pnl"]))
    after_delta = Decimal("0")
    for row in rows:
        if economic_close_exists(dataset, row):
            summary["existing"] += 1
            continue
        try:
            hit = match_lifecycle(
                history,
                symbol=str(row.get("symbol") or ""),
                direction=str(row.get("direction") or ""),
                opened_at_ms=_opened_at_ms(row),
                size=float(row.get("confirmed_position_size") or row.get("position_size")),
                exchange_position_id=row.get("exchange_position_id"),
            )
            if hit is None:
                raise CloseReconciliationUnavailable("no unambiguous match")
            economics = economics_from_history(hit)
        except (AmbiguousLifecycle, CloseReconciliationUnavailable, TypeError, ValueError):
            summary["ambiguous_or_missing"] += 1
            continue
        summary["safe"] += 1
        after_delta += Decimal(str(economics.net_profit))
        if apply:
            append_exchange_truth_close(
                position=row,
                economics=economics,
                close_reason=str(row.get("close_reason") or row.get("result") or "migration_recovery"),
                dataset_path=dataset,
            )
            summary["applied"] += 1
    summary["before_total"] = str(before)
    summary["expected_after_delta"] = str(after_delta)
    summary["expected_after_total"] = str(before + after_delta)
    summary["writes_enabled"] = apply
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="logs/trade_dataset_v2.csv")
    parser.add_argument("--apply", action="store_true", help="append safe local economic rows")
    args = parser.parse_args()
    client = BitgetRestClient(settings=get_settings())
    result = audit(dataset=args.dataset, history=_history(client), apply=args.apply)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
