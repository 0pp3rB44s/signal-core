"""Ranked-plan observability: why this plan won and that one did not.

Nothing here selects, scores, orders, filters or executes anything. It records
what ``select_execution_winner`` already decided.

WHY THIS EXISTS
---------------
Production logs only the winner. Every question about the ranker has therefore
had to be answered by rebuilding the ranking offline from ``trade_plans.csv``
plus ``market_context.csv``, and that reconstruction has been wrong twice:
once because the execution-score map was incomplete, once because the Runner
logs are local time while the CSVs are UTC. Both were caught, but only because
someone went looking. A ranking that is reconstructed is a ranking that can
silently disagree with the one that traded.

The one structural finding so far -- that a true economic tie falls through to
``strategy.lower()`` and ``low_vol_reclaim`` wins it alphabetically -- was found
that way, and could not be confirmed live because the losing plans are never
written down.

WHAT IS AUTHORITATIVE HERE
--------------------------
The four ranking keys come straight off the ``RankedPlan`` objects the selector
returned. They are not recomputed, because a second implementation of the same
arithmetic is exactly how telemetry starts lying about runtime.

``rank`` is the index in ``selection.ranked``, which is the selector's own sort
order, so ``rank == 0`` is by construction the plan that ``selection.winner``
returns.

Everything under ``diagnostic`` is parsed from the plan's notes and is *not*
what the selector used. It is separated by name so a future reader cannot
mistake a diagnostic echo for a ranking input.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from telemetry.csv_rotation import rotate_if_needed
from telemetry.safe_io import file_lock

#: One line per selection cycle, next to the other research feeds.
DEFAULT_PATH = "logs/ranked_plans.jsonl"

#: Rotation matches the CSV feeds so the Runner -> Work Mac sync, which
#: allowlists ``ranked_plans.jsonl*``, picks up the rotated segments too.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

_FLOAT = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))"


def _note_text(plan: Any) -> str:
    return " | ".join(
        str(v)
        for v in [*(getattr(plan, "notes", None) or []), *(getattr(plan, "reasons", None) or [])]
    )


def _marker_float(text: str, marker: str) -> float | None:
    match = re.search(re.escape(marker) + r"\s*" + _FLOAT, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _diagnostic(plan: Any) -> dict[str, Any]:
    """Echo of pre-entry fields. Never a ranking input -- see module docstring."""
    text = _note_text(plan)
    long_q = _marker_float(text, "entry_quality_long=")
    short_q = _marker_float(text, "entry_quality_short=")
    direction = str(getattr(plan, "direction", "") or "").upper()
    selected = long_q if direction == "LONG" else short_q if direction == "SHORT" else None
    return {
        "plan_score": _safe_float(getattr(plan, "score", None)),
        "planner_entry_quality": _marker_float(text, "planner_entry_quality="),
        "entry_quality_long": long_q,
        "entry_quality_short": short_q,
        # Named for the side actually being traded. Deliberately not
        # max(long, short): that conflation was a real production defect.
        "selected_direction_entry_quality": selected,
        "spread_bps": _marker_float(text, "spread_bps="),
        "participation_score": _marker_float(text, "participation_score="),
    }


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


class RankedPlanLogger:
    """Append one JSONL row per selection cycle. Failure never reaches trading."""

    def __init__(self, path: str | Path = DEFAULT_PATH, log: Any = None,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.log = log
        self.max_bytes = max_bytes

    def build_row(self, scan_id: Any, timestamp: str, selection: Any) -> dict[str, Any]:
        ranked = list(getattr(selection, "ranked", ()) or ())
        winner = getattr(selection, "winner", None)
        winner_id = getattr(winner, "plan_id", None)
        plans = []
        for index, row in enumerate(ranked):
            plan = row.plan
            plans.append({
                "rank": index,
                "selected": bool(winner_id is not None and plan.plan_id == winner_id),
                "plan_id": plan.plan_id,
                "symbol": str(plan.symbol or "").upper(),
                "direction": str(plan.direction or "").upper(),
                "strategy": str(plan.strategy or "").lower(),
                "verdict": str(getattr(plan, "verdict", "") or "").upper(),
                # The selector's own values, not a second computation.
                "ranking_keys": {
                    "execution_score": row.execution_score,
                    "expectancy": row.expectancy,
                    "setup_quality": row.setup_quality,
                    "liquidity_spread_quality": row.liquidity_spread_quality,
                },
                "diagnostic": _diagnostic(plan),
            })
        rejected = [
            {"symbol": r.symbol, "plan_id": r.plan_id, "reason": r.reason}
            for r in (getattr(selection, "rejected", ()) or ())
        ]
        return {
            "scan_id": scan_id,
            "timestamp": timestamp,
            "ranked_count": len(plans),
            "rejected_count": len(rejected),
            "winner_plan_id": winner_id,
            "plans": plans,
            "rejected": rejected,
        }

    def append(self, scan_id: Any, timestamp: str, selection: Any) -> bool:
        try:
            row = self.build_row(scan_id, timestamp, selection)
        except Exception as exc:  # noqa: BLE001
            self._warn("RANKED_PLANS_ROW_BUILD_FAILED | scan=%s | error=%s", scan_id, exc)
            return False
        try:
            directory = self.path.parent
            if str(directory):
                os.makedirs(directory, exist_ok=True)
            rotate_if_needed(self.path, max_bytes=self.max_bytes)
            line = json.dumps(row, default=str, sort_keys=True)
            # Append-only and line-atomic. No fsync: this sits in the scan loop
            # and one durable write per cycle buys nothing that a lost tail of a
            # research feed would not survive.
            with file_lock(self.path):
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:  # noqa: BLE001
            self._warn("RANKED_PLANS_WRITE_FAILED | scan=%s | error=%s", scan_id, exc)
            return False
        return True

    def _warn(self, fmt: str, *args: Any) -> None:
        if self.log is not None:
            try:
                self.log.warning(fmt, *args)
            except Exception:  # noqa: BLE001
                pass


__all__ = ["RankedPlanLogger", "DEFAULT_PATH", "DEFAULT_MAX_BYTES"]
