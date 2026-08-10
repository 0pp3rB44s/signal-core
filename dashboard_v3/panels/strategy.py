"""Per-strategy funnel and economics — "which strategy earns, and where do the rest die?".

The bot runs five strategies and trades almost exclusively one of them. Nothing
in the product currently shows that, so the question "why does momentum_breakout
produce 500 candidates and almost no trades?" cannot be answered from a screen.

LINEAGE, AND WHY THERE ARE THREE BLOCKS INSTEAD OF ONE FUNNEL
-------------------------------------------------------------
Three stages can be joined and three cannot:

* ``strategy_candidates.csv`` and ``trade_plans.csv`` share ``candidate_id``
  (615/615 in the 2026-08-10 snapshot), so candidate -> plan -> executable is
  one cohort and a conversion rate between them means something.
* ``trade_dataset_v2.csv`` carries ``position_lifecycle_id`` and exchange ids,
  and **no ``plan_id`` or ``candidate_id``**. A plan therefore cannot be traced
  to the position it became.
* "Selected" is not written down anywhere at all. Only the winner reaches
  ``agent.log``, and the losing plans of a cycle are never recorded.

So executable -> selected -> opened is not a cohort, and dividing one by the
other would invent a number. Those stages are reported side by side under an
explicit ``INCOMPLETE_LINEAGE`` marker instead. ``opened -> closed`` is a real
cohort again, joined on the lifecycle id.

ECONOMICS
---------
Only authoritative LIVE closes. ``is_displayable_close`` is the same predicate
the performance page uses -- imported, not reimplemented -- so the two screens
cannot drift apart; a test pins the totals equal. Provisional closes, recovery
rows and low-confidence rows are separated, never summed in.
"""

from __future__ import annotations

import collections
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status
from telemetry.close_record_sources import is_displayable_close

#: Production strategies, in the order the funnel is read. Listed explicitly so
#: a strategy that stops producing rows still shows up as a zero rather than
#: vanishing from the page.
KNOWN_STRATEGIES = [
    "low_vol_reclaim",
    "momentum_breakout",
    "momentum_breakdown",
    "trend_continuation",
    "liquidity_sweep_reversal",
]

#: Present in code, disabled by configuration. Shown as dormant rather than
#: omitted, so "no rows" is not mistaken for "no such strategy".
DORMANT_STRATEGIES = ["adaptive_momentum_continuation"]

#: Fixed, conservative sample bands. Not fitted to any result.
SAMPLE_BANDS = ((0, "NO_DATA"), (1, "TINY_SAMPLE"), (10, "DESCRIPTIVE"), (30, "REASONABLE_SAMPLE"))

#: Below this the fee share of gross is not reported: when gross is near zero
#: the ratio explodes and says nothing.
MIN_ABS_GROSS_FOR_FEE_SHARE = 0.01


def evidence_label(n: int) -> str:
    label = "NO_DATA"
    for threshold, name in SAMPLE_BANDS:
        if n >= threshold:
            label = name
    return label


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def _ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _strategy(row: dict[str, Any]) -> str:
    return str(row.get("strategy") or "unknown").strip().lower() or "unknown"


def _rows(rel: str) -> tuple[list[dict[str, Any]], Any]:
    loaded = src.load_csv(rel, limit=None)
    return (loaded.value if isinstance(loaded.value, list) else []), loaded


# --- funnel -----------------------------------------------------------------


def _reason_counts(plans: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Blocking reasons, digits masked so "score 91 < 95" and "score 88 < 95" group."""
    counter: collections.Counter[str] = collections.Counter()
    for plan in plans:
        text = " | ".join(str(plan.get(k) or "") for k in ("reasons", "notes"))
        for part in text.split("|"):
            part = part.strip()
            if not part or "=" in part.split(" ")[0][:28]:
                continue  # key=value telemetry, not a blocking reason
            counter[re.sub(r"[-+]?\d+(?:\.\d+)?", "<N>", part)[:90]] += 1
    total = max(1, len(plans))
    return [
        {"reason": reason, "count": count, "pct": round(count / total * 100, 1)}
        for reason, count in counter.most_common(limit)
    ]


def build_funnel() -> dict[str, Any]:
    signals = SignalSet()
    candidates, cand_loaded = _rows("logs/strategy_candidates.csv")
    plans, plan_loaded = _rows("logs/trade_plans.csv")
    dataset, ds_loaded = _rows("logs/trade_dataset_v2.csv")

    by_cand = collections.Counter(_strategy(r) for r in candidates)
    plans_by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for plan in plans:
        plans_by[_strategy(plan)].append(plan)

    # Positions: one row per lifecycle, so a re-emitted OPEN cannot inflate it.
    opened: dict[str, set[str]] = collections.defaultdict(set)
    closed: dict[str, set[str]] = collections.defaultdict(set)
    for row in dataset:
        lifecycle = str(row.get("position_lifecycle_id") or "")
        if not lifecycle:
            continue
        event = str(row.get("event_type") or "").strip().upper()
        if event == "OPEN":
            opened[_strategy(row)].add(lifecycle)
        elif event == "CLOSE":
            closed[_strategy(row)].add(lifecycle)

    seen = set(by_cand) | set(plans_by) | set(opened) | set(closed)
    ordered = KNOWN_STRATEGIES + [s for s in sorted(seen) if s not in KNOWN_STRATEGIES
                                  and s not in DORMANT_STRATEGIES]
    rows = []
    for name in ordered + DORMANT_STRATEGIES:
        mine = plans_by.get(name, [])
        executable = [p for p in mine if str(p.get("verdict") or "").upper() == "EXECUTABLE"]
        blocked = [p for p in mine if str(p.get("verdict") or "").upper() != "EXECUTABLE"]
        n_cand = by_cand.get(name, 0)
        n_open, n_closed = len(opened.get(name, ())), len(closed.get(name, ()))
        rows.append({
            "strategy": name,
            "dormant": name in DORMANT_STRATEGIES,
            "candidates": n_cand,
            "plans": len(mine),
            "executable": len(executable),
            "blocked": len(blocked),
            "opened": n_open,
            "closed": n_closed,
            # Same cohort (candidate_id joins these): a rate is meaningful.
            "plan_to_executable_pct": (round(len(executable) / len(mine) * 100, 1) if mine else None),
            # Same cohort (lifecycle id joins these).
            "opened_to_closed_pct": (round(n_closed / n_open * 100, 1) if n_open else None),
            # Deliberately absent: executable -> selected -> opened. No shared
            # key exists and "selected" is not recorded at all.
            "executable_to_opened_pct": None,
            "lineage": "INCOMPLETE_LINEAGE",
            "top_reasons": _reason_counts(blocked),
        })

    if not plans:
        signals.add(Signal("plans", "Plans", Status.UNKNOWN, "logs/trade_plans.csv leverde geen rijen"))
    return {
        "signals": signals,
        "status": signals.status,
        "rows": rows,
        "window": _window(candidates + plans),
        "lineage_note": (
            "Executable → selected → opened is niet één cohort: trade_dataset_v2.csv "
            "draagt geen plan_id of candidate_id, en de niet-gekozen plannen van een "
            "cycle worden nergens vastgelegd. Die stadia staan naast elkaar, zonder "
            "conversiepercentage."
        ),
        "sources": [
            {"file": "logs/strategy_candidates.csv", "prov": cand_loaded.provenance},
            {"file": "logs/trade_plans.csv", "prov": plan_loaded.provenance},
            {"file": "logs/trade_dataset_v2.csv", "prov": ds_loaded.provenance},
        ],
    }


def _window(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stamps = [t for t in (_ts(r.get("timestamp")) for r in rows) if t]
    if not stamps:
        return {"first": None, "last": None, "age_seconds": None}
    last = max(stamps)
    return {
        "first": min(stamps).isoformat(),
        "last": last.isoformat(),
        "age_seconds": max(0.0, (datetime.now(timezone.utc) - last).total_seconds()),
    }


# --- economics --------------------------------------------------------------


def _economics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    gross = sum(t["gross"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    funding = sum(t["funding"] for t in trades)
    net = sum(t["net"] for t in trades)
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    tp1_known = [t for t in trades if t["tp1"] is not None]
    tp1_hits = sum(1 for t in tp1_known if t["tp1"])
    avg_win = (gross_win / len(wins)) if wins else None
    avg_loss = (-gross_loss / len(losses)) if losses else None
    return {
        "n": n,
        "evidence": evidence_label(n),
        "gross": round(gross, 6),
        "fees": round(fees, 6),
        "funding": round(funding, 6),
        "net": round(net, 6),
        # How much of the gross result the cost structure consumed. Below the
        # floor the ratio explodes on a rounding artefact and is withheld.
        # Above 100% a percentage reads like a bug, so it is expressed as a
        # multiple instead: "fees were 18.3x the entire gross result" is the
        # actual finding, and it should look like one.
        "fees_pct_of_abs_gross": (
            round(fees / abs(gross) * 100, 1)
            if abs(gross) >= MIN_ABS_GROSS_FOR_FEE_SHARE and fees <= abs(gross)
            else None
        ),
        "fees_multiple_of_abs_gross": (
            round(fees / abs(gross), 1)
            if abs(gross) >= MIN_ABS_GROSS_FOR_FEE_SHARE and fees > abs(gross)
            else None
        ),
        "expectancy": round(net / n, 6) if n else None,
        "winrate": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "avg_win": round(avg_win, 6) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 6) if avg_loss is not None else None,
        "payoff": (round(avg_win / abs(avg_loss), 3) if avg_win and avg_loss else None),
        "tp1_hits": tp1_hits,
        "tp1_measured": len(tp1_known),
        "tp1_rate": (round(tp1_hits / len(tp1_known) * 100, 1) if tp1_known else None),
    }


def _truthy(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def build_economics(window_days: int = 30) -> dict[str, Any]:
    signals = SignalSet()
    dataset, loaded = _rows("logs/trade_dataset_v2.csv")
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    live: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    excluded = collections.Counter()
    seen_lifecycles: set[str] = set()

    for row in dataset:
        status = str(row.get("status") or "").upper()
        if not status.startswith("CLOSED"):
            continue
        # Same predicate the performance page uses. Never a second definition.
        if not is_displayable_close(row):
            excluded["provisional_or_non_economic"] += 1
            continue
        lifecycle = str(row.get("position_lifecycle_id") or "")
        if lifecycle and lifecycle in seen_lifecycles:
            excluded["duplicate_lifecycle"] += 1
            continue
        when = _ts(row.get("closed_at")) or _ts(row.get("timestamp"))
        if when is None:
            excluded["undated"] += 1
            continue
        if when < cutoff:
            continue
        strategy = _strategy(row)
        confidence = str(row.get("data_confidence") or "").upper()
        if strategy == "recovered_exchange_position" or confidence in {"LOW_CONFIDENCE", "UNKNOWN"}:
            excluded["recovery_or_low_confidence"] += 1
            continue
        if lifecycle:
            seen_lifecycles.add(lifecycle)
        live[strategy].append({
            "gross": _f(row.get("gross_pnl")),
            "fees": _f(row.get("fees")),
            "funding": _f(row.get("funding")),
            "net": _f(row.get("net_pnl")),
            "tp1": _truthy(row.get("tp1_hit")),
            "closed_at": when,
        })

    rows = []
    for name in KNOWN_STRATEGIES + [s for s in sorted(live) if s not in KNOWN_STRATEGIES]:
        stats = _economics(live.get(name, []))
        stats["strategy"] = name
        rows.append(stats)
    totals = _economics([t for group in live.values() for t in group])

    stamps = [t["closed_at"] for group in live.values() for t in group]
    if not stamps:
        signals.add(Signal("live_closes", "LIVE closes", Status.UNKNOWN,
                           "geen authoritative LIVE closes in het venster"))
    return {
        "signals": signals,
        "status": signals.status,
        "rows": rows,
        "totals": totals,
        "window_days": window_days,
        "window": {
            "first": min(stamps).isoformat() if stamps else None,
            "last": max(stamps).isoformat() if stamps else None,
        },
        "excluded": dict(excluded),
        "cohort_note": (
            "Alleen authoritative LIVE closes. Provisional closes, recovery-rijen en "
            "low-confidence rijen zijn uitgesloten en worden nooit opgeteld."
        ),
        "source": {"file": "logs/trade_dataset_v2.csv", "prov": loaded.provenance},
    }


# --- ranking (awaits the ranked-plan feed) ----------------------------------

RANKED_PLANS_PATH = "logs/ranked_plans.jsonl"
NOT_AVAILABLE = "RANKED_PLAN_TELEMETRY_NOT_AVAILABLE"


def build_ranking() -> dict[str, Any]:
    """Boundary for the future ranking view.

    The feed is produced by a separate, unmerged change. Until it exists this
    reports its absence and reconstructs nothing: rebuilding the ranking offline
    is exactly what has already produced two wrong answers.
    """
    signals = SignalSet()
    loaded = src.load_jsonl_tail(RANKED_PLANS_PATH)
    rows = loaded.value if isinstance(loaded.value, list) else []
    if not rows:
        signals.add(Signal("ranked_plans", "Ranked plans", Status.UNKNOWN, NOT_AVAILABLE))
        return {
            "signals": signals,
            "status": signals.status,
            "available": False,
            "state": NOT_AVAILABLE,
            "detail": (
                f"{RANKED_PLANS_PATH} ontbreekt of is leeg. De gerankte plannen "
                "worden niet gereconstrueerd uit andere bronnen."
            ),
            "cycles": [],
            "source": {"file": RANKED_PLANS_PATH, "prov": loaded.provenance},
        }
    return {
        "signals": signals,
        "status": signals.status,
        "available": True,
        "state": "AVAILABLE",
        "detail": f"{len(rows)} cycli in de feed.",
        "cycles": rows[-50:],
        "source": {"file": RANKED_PLANS_PATH, "prov": loaded.provenance},
    }


def build() -> dict[str, Any]:
    return {
        "funnel": build_funnel(),
        "economics": build_economics(),
        "ranking": build_ranking(),
    }
