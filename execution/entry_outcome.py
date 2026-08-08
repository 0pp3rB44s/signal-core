"""Post-fill outcome sampling, kept physically apart from the entry record.

The separation is the point. ``entry_routing.py`` records only what was knowable
before the position existed; this module records only what became knowable
after. They share a ``lifecycle_id`` and nothing else, and they write to
different files, so a consumer building a pre-entry gate or a training set from
``logs/entry_routing.jsonl`` cannot reach a post-fill field even by accident.
Joining the two is a deliberate act performed by an analyst, not a default.

The tracker samples prices the caller already holds. It issues no request of its
own: the position monitor polls marks every few seconds regardless, so
observability here costs nothing on the exchange and cannot delay an order,
a protection placement, or a close.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from execution.entry_routing import position_return_bps, utc_now_iso_ms

#: Horizons in seconds. 60 is the longest, so a lifecycle retires after that.
RETURN_HORIZONS_S = (10, 30, 60)
MAX_HORIZON_S = max(RETURN_HORIZONS_S)


@dataclass
class _Sample:
    at: datetime
    price: float


@dataclass
class _Tracked:
    lifecycle_id: str
    symbol: str
    direction: str
    fill_price: float
    filled_at: datetime
    plan_id: str = ""
    samples: list[_Sample] = field(default_factory=list)

    def elapsed_s(self, now: datetime) -> float:
        return (now - self.filled_at).total_seconds()


class EntryOutcomeTracker:
    """Collects post-fill price samples per entry lifecycle.

    Not a scheduler: ``observe`` is called by whatever loop already runs, and
    ``flush_due`` is called by the same loop. If that loop stops, sampling stops
    and the affected lifecycles are written with NULL horizons rather than
    invented ones.
    """

    _write_lock = threading.Lock()

    def __init__(self, *, log: Any = None, path: str = "logs/entry_outcomes.jsonl") -> None:
        self.log = log
        self.path = path
        self._tracked: dict[str, _Tracked] = {}

    def arm(
        self,
        *,
        lifecycle_id: str,
        symbol: str,
        direction: str,
        fill_price: float,
        filled_at: datetime | None = None,
        plan_id: str = "",
    ) -> None:
        """Begin sampling one lifecycle. Re-arming the same id is ignored."""
        if not lifecycle_id or lifecycle_id in self._tracked:
            return
        try:
            price = float(fill_price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        self._tracked[lifecycle_id] = _Tracked(
            lifecycle_id=lifecycle_id,
            symbol=str(symbol).upper(),
            direction=str(direction).upper(),
            fill_price=price,
            filled_at=filled_at or datetime.now(timezone.utc),
            plan_id=plan_id,
        )

    def observe(self, symbol: str, price: Any, at: datetime | None = None) -> None:
        """Record one price the caller already had. Junk is dropped silently."""
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        moment = at or datetime.now(timezone.utc)
        wanted = str(symbol).upper()
        for tracked in self._tracked.values():
            if tracked.symbol == wanted and tracked.elapsed_s(moment) <= MAX_HORIZON_S:
                tracked.samples.append(_Sample(at=moment, price=value))

    def flush_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Write and retire every lifecycle past the longest horizon."""
        moment = now or datetime.now(timezone.utc)
        due = [t for t in self._tracked.values() if t.elapsed_s(moment) >= MAX_HORIZON_S]
        written: list[dict[str, Any]] = []
        for tracked in due:
            row = self._build_row(tracked)
            if self._write(row):
                written.append(row)
            self._tracked.pop(tracked.lifecycle_id, None)
        return written

    # --- computation -------------------------------------------------------

    def _nearest_at_or_after(self, tracked: _Tracked, horizon_s: int) -> float | None:
        """The first sample at or after the horizon, or None if none reached it.

        Deliberately not interpolated and not back-filled from an earlier
        sample: a 60s return computed from a 12s price is a fabrication wearing
        a real number's clothes.
        """
        target = tracked.filled_at + timedelta(seconds=horizon_s)
        candidates = [s for s in tracked.samples if s.at >= target]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.at).price

    def _build_row(self, tracked: _Tracked) -> dict[str, Any]:
        returns: dict[str, float | None] = {}
        for horizon in RETURN_HORIZONS_S:
            price = self._nearest_at_or_after(tracked, horizon)
            returns[f"post_fill_return_{horizon}s_bps"] = position_return_bps(
                tracked.direction, tracked.fill_price, price
            )

        within = [
            s for s in tracked.samples
            if (s.at - tracked.filled_at).total_seconds() <= MAX_HORIZON_S
        ]
        excursions = [
            position_return_bps(tracked.direction, tracked.fill_price, s.price)
            for s in within
        ]
        excursions = [e for e in excursions if e is not None]

        return {
            "schema_version": 1,
            "record_type": "entry_outcome",
            "written_at": utc_now_iso_ms(),
            "lifecycle_id": tracked.lifecycle_id,
            "plan_id": tracked.plan_id,
            "symbol": tracked.symbol,
            "direction": tracked.direction,
            "fill_price": tracked.fill_price,
            "filled_at": tracked.filled_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "sample_count": len(within),
            **returns,
            "post_fill_mfe_60s_bps": max(excursions) if excursions else None,
            "post_fill_mae_60s_bps": min(excursions) if excursions else None,
        }

    def _write(self, row: dict[str, Any]) -> bool:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            line = json.dumps(row, default=str, sort_keys=True)
            with self._write_lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except Exception as exc:  # noqa: BLE001 - observability must not raise
            if self.log is not None:
                self.log.warning(
                    "ENTRY_OUTCOME_WRITE_FAILED | %s | lifecycle=%s | error=%s",
                    row.get("symbol"), row.get("lifecycle_id"), exc,
                )
            return False
        return True
