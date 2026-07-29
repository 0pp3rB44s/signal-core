"""The decision funnel — the answer to "why is the bot not trading?".

This is the most important panel in the product. It reconstructs the pipeline
from the funnel event log and names the single decisive gate, rather than
leaving the owner to infer it from a wall of reasons.
"""

from __future__ import annotations

import collections
from datetime import datetime, timezone
from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, Status

#: Pipeline order. Anything the log emits that is not listed stays out of the
#: funnel chart rather than being silently folded into a neighbouring stage.
STAGES = [
    ("DETECTOR_ATTEMPT", "Detector attempts"),
    ("DETECTOR_DECISION", "Detections"),
    ("SELECTOR_DECISION", "Selector passes"),
    ("SCORING_DECISION", "Scoring passes"),
    ("RISK_DECISION", "Risk passes"),
    ("PLANNER_DECISION", "Plans built"),
    ("EXECUTABLE_DECISION", "Executable"),
    ("ORDER_SUBMIT", "Orders submitted"),
    ("ORDER_FILL", "Orders filled"),
]

#: Human wording for the machine reason codes.
REASON_LABELS = {
    "NO_DETECTION": "No setup detected",
    "SYMBOL_EXPECTANCY_PAUSE": "Symbol paused by expectancy kill-switch",
    "EXPECTANCY_BLOCK": "Strategy expectancy negative",
    "HTF_OPPOSITION": "Higher-timeframe trend opposes the entry",
    "EXECUTION_COST": "Spread / entry quality too expensive",
    "RISK_BLOCKED": "Risk gate blocked the candidate",
    "PLAN_BLOCKED": "Plan blocked downstream of risk",
}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build(session_start: datetime | None = None) -> dict[str, Any]:
    loaded = src.load_jsonl_tail("data_store/funnel_events.jsonl", limit=6000)
    events = loaded.value if isinstance(loaded.value, list) else []

    if session_start:
        scoped = []
        for e in events:
            ts = _parse_ts(e.get("event_timestamp_utc"))
            if ts and ts >= session_start:
                scoped.append(e)
        events = scoped

    totals: dict[str, int] = collections.Counter()
    fails: dict[str, int] = collections.Counter()
    reasons_by_stage: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    secondary: collections.Counter = collections.Counter()
    combos: collections.Counter = collections.Counter()
    strategies: collections.Counter = collections.Counter()
    symbols: collections.Counter = collections.Counter()
    first_ts = last_ts = None

    for e in events:
        stage = str(e.get("event_type") or "")
        totals[stage] += 1
        ts = _parse_ts(e.get("event_timestamp_utc"))
        if ts:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts
        if str(e.get("pass_fail") or "").upper() == "FAIL":
            fails[stage] += 1
            code = str(e.get("primary_reason_code") or "UNSPECIFIED")
            reasons_by_stage[stage][code] += 1
        if stage == "RISK_DECISION":
            codes = e.get("secondary_reason_codes") or []
            if isinstance(codes, list):
                for c in codes:
                    secondary[str(c)] += 1
                if codes:
                    combos[tuple(sorted(str(c) for c in codes))] += 1
        if e.get("strategy"):
            strategies[str(e["strategy"])] += 1
        if e.get("symbol"):
            symbols[str(e["symbol"])] += 1

    rows = []
    previous_pass: int | None = None
    for key, label in STAGES:
        total = totals.get(key, 0)
        failed = fails.get(key, 0)
        passed = max(0, total - failed)
        conversion = (passed / previous_pass * 100.0) if previous_pass else None
        rows.append({
            "key": key,
            "label": label,
            "total": total,
            "failed": failed,
            "passed": passed,
            "observed": key in totals,
            "conversion_pct": conversion,
            "pass_pct": (passed / total * 100.0) if total else None,
            "top_reasons": [
                {"code": c, "label": REASON_LABELS.get(c, c), "count": n}
                for c, n in reasons_by_stage.get(key, collections.Counter()).most_common(4)
            ],
        })
        if total:
            previous_pass = passed

    # --- decisive gate: the first stage that admits nothing --------------
    decisive = None
    for row in rows:
        if row["total"] > 0 and row["passed"] == 0:
            decisive = row
            break

    risk_total = totals.get("RISK_DECISION", 0)
    blockers = [
        {
            "code": code,
            "label": REASON_LABELS.get(code, code),
            "count": n,
            "share_pct": (n / risk_total * 100.0) if risk_total else None,
        }
        for code, n in secondary.most_common(8)
    ]

    # How many decisions would survive if ONLY the top blocker were lifted?
    sole_blocker_releases = 0
    top_code = blockers[0]["code"] if blockers else None
    if top_code:
        for combo, n in combos.items():
            if set(combo) <= {top_code, "EXPECTANCY_BLOCK"}:
                sole_blocker_releases += n

    signal = Signal(
        "funnel", "Trade funnel",
        Status.BLOCKED if decisive else (Status.HEALTHY if events else Status.UNKNOWN),
        (f"{decisive['label']} admits 0 of {decisive['total']}" if decisive
         else ("pipeline flowing" if events else "no funnel events in window")),
        "The decisive gate is the first stage that lets nothing through.",
    )

    return {
        "rows": rows,
        "decisive": decisive,
        "blockers": blockers,
        "combos": [
            {"codes": list(c), "count": n}
            for c, n in combos.most_common(6)
        ],
        "sole_blocker_releases": sole_blocker_releases,
        "top_blocker": top_code,
        "strategies": strategies.most_common(8),
        "symbols": symbols.most_common(8),
        "event_count": len(events),
        "window_start": first_ts,
        "window_end": last_ts,
        "window_hours": ((last_ts - first_ts).total_seconds() / 3600.0)
                        if first_ts and last_ts else None,
        "provenance": loaded.provenance,
        "signal": signal,
        "status": signal.status,
    }


def score_distribution(limit: int = 4000) -> dict[str, Any]:
    """Plan scores versus their verdicts — exposes blocked high-conviction setups."""
    loaded = src.load_csv("logs/trade_plans.csv", limit=limit)
    rows = loaded.value if isinstance(loaded.value, list) else []
    buckets = collections.Counter()
    verdicts = collections.Counter()
    blocked_high: list[dict[str, Any]] = []
    scores: list[float] = []

    for row in rows:
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            continue
        verdict = str(row.get("verdict") or "UNKNOWN").upper()
        verdicts[verdict] += 1
        scores.append(score)
        buckets[int(score // 10) * 10] += 1
        if score >= 90 and verdict != "EXECUTABLE":
            blocked_high.append({
                "symbol": row.get("symbol"),
                "strategy": row.get("strategy"),
                "direction": row.get("direction"),
                "score": score,
                "verdict": verdict,
            })

    scores.sort()
    return {
        "buckets": sorted(buckets.items()),
        "verdicts": dict(verdicts),
        "count": len(scores),
        "min": scores[0] if scores else None,
        "median": scores[len(scores) // 2] if scores else None,
        "max": scores[-1] if scores else None,
        "blocked_high_count": len(blocked_high),
        "blocked_high": blocked_high[-12:],
        "provenance": loaded.provenance,
    }


__all__ = ["REASON_LABELS", "STAGES", "build", "score_distribution"]
