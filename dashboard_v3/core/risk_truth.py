"""Weekly risk figures that match the kill-switch, rather than resembling it.

The dashboard previously recomputed the weekly loss with `is_displayable_close`
over whatever journal rows a panel happened to hold. `RiskManager._weekly_realized_pnl`
— the path that actually blocks trading — uses `is_economic_close`, reads the
rotated segment as well as the active file, and falls back to a synthetic
identity when `position_lifecycle_id` is empty.

Those are not cosmetic differences:

* `is_displayable_close` is a DENYLIST (an empty `sync_source` passes) while
  `is_economic_close` is an ALLOWLIST (an empty `sync_source` fails). Displayable
  is strictly wider, so the old panel counted rows the kill-switch ignores.
* Skipping `trade_dataset_v2.csv.1` drops every close that rotated out.
* Deduplicating on lifecycle id alone double-counts legacy rows that have none.

The net error was not even signed consistently, which is the worst property a
risk number can have: the operator saw a figure that was not the figure gating
their account. This module mirrors the authoritative implementation field for
field so the two cannot drift silently, and states its own provenance.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telemetry.close_record_sources import ECONOMIC_CLOSE_EVENT_TYPES, is_economic_close

#: Same order as RiskManager: rotated segment first, then the active file.
DATASET_RELS = ("logs/trade_dataset_v2.csv.1", "logs/trade_dataset_v2.csv")
WINDOW_DAYS = 7


@dataclass(frozen=True)
class WeeklyRisk:
    """Every field the operator needs, plus how much to trust it."""

    realized_pnl: float | None
    counted_trades: int
    skipped_non_economic: int
    equity: float | None
    loss_pct: float | None
    freeze_threshold_pct: float | None
    freeze_active: bool | None
    files_read: tuple[str, ...]
    files_missing: tuple[str, ...]
    window_start: datetime
    window_end: datetime

    @property
    def headroom_pct(self) -> float | None:
        if self.loss_pct is None or self.freeze_threshold_pct is None:
            return None
        return self.freeze_threshold_pct - self.loss_pct

    @property
    def usable(self) -> bool:
        """False when no dataset file was readable — the caller must render
        UNKNOWN rather than a confident 0.0, which would read as 'no loss'."""
        return bool(self.files_read)


def compute_weekly_risk(
    base_path: Path,
    *,
    equity: float | None,
    freeze_threshold_pct: float | None,
    now: datetime | None = None,
) -> WeeklyRisk:
    """Rolling 7-day realized net PnL, computed exactly as the kill-switch does."""
    now = now or datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=WINDOW_DAYS)
    cutoff = cutoff_dt.isoformat()

    total = 0.0
    counted: set[str] = set()
    skipped_non_economic = 0
    read: list[str] = []
    missing: list[str] = []

    for rel in DATASET_RELS:
        path = base_path / rel
        if not path.exists():
            missing.append(rel)
            continue
        read.append(rel)
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not is_economic_close(row):
                    if str(row.get("event_type") or "").upper() in ECONOMIC_CLOSE_EVENT_TYPES:
                        skipped_non_economic += 1
                    continue
                closed_at = str(row.get("closed_at") or row.get("timestamp") or "")
                if closed_at < cutoff:
                    continue
                identity = str(row.get("position_lifecycle_id") or "").strip() or "{}|{}|{}".format(
                    str(row.get("symbol") or "").upper(),
                    str(row.get("direction") or "").upper(),
                    closed_at[:19],
                )
                if identity in counted:
                    continue
                raw = row.get("net_pnl") or row.get("pnl") or 0
                try:
                    total += float(raw)
                except (TypeError, ValueError):
                    continue
                counted.add(identity)

    realized: float | None = total if read else None

    loss_pct: float | None = None
    if realized is not None and equity and equity > 0:
        loss_pct = abs(realized) / equity * 100.0 if realized < 0 else 0.0

    freeze_active: bool | None = None
    if loss_pct is not None and freeze_threshold_pct:
        freeze_active = loss_pct >= freeze_threshold_pct

    return WeeklyRisk(
        realized_pnl=realized,
        counted_trades=len(counted),
        skipped_non_economic=skipped_non_economic,
        equity=equity,
        loss_pct=loss_pct,
        freeze_threshold_pct=freeze_threshold_pct,
        freeze_active=freeze_active,
        files_read=tuple(read),
        files_missing=tuple(missing),
        window_start=cutoff_dt,
        window_end=now,
    )
