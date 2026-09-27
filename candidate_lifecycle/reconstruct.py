from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from forward_paper.store import ForwardPaperEventStore
from telemetry.funnel import FunnelEventStore


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def reconstruct_candidate_lifecycles(
    funnel_path: str | Path,
    forward_events_path: str | Path,
    outcomes_path: str | Path,
) -> dict[str, Any]:
    funnel_events = FunnelEventStore(funnel_path).read_events()
    forward_events = ForwardPaperEventStore(forward_events_path).read_events()
    outcomes_file = Path(outcomes_path)
    outcomes = list(csv.DictReader(outcomes_file.open(encoding="utf-8"))) if outcomes_file.exists() else []

    candidate_ids = sorted({str(event["candidate_id"]) for event in funnel_events if event.get("candidate_id")})
    records: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        stages = [event for event in funnel_events if event.get("candidate_id") == candidate_id]
        paper = [event for event in forward_events if event.get("candidate_id") == candidate_id]
        linked_outcomes = [row for row in outcomes if row.get("candidate_id") == candidate_id]
        plan_ids = sorted({
            str(event.get("plan_id") or (event.get("details") or {}).get("plan_id"))
            for event in stages + paper
            if event.get("plan_id") or (event.get("details") or {}).get("plan_id")
        })
        trade_ids = sorted({
            str(event.get("trade_id") or (event.get("details") or {}).get("trade_id"))
            for event in stages + paper
            if event.get("trade_id") or (event.get("details") or {}).get("trade_id")
        })
        records.append({
            "identity_status": "LINKED",
            "candidate_id": candidate_id,
            "strategy": stages[0]["strategy"] if stages else "",
            "symbol": stages[0]["symbol"] if stages else "",
            "direction": stages[0]["direction"] if stages else "",
            "candidate_candle_open_timestamp": stages[0].get("candle_open_timestamp", "") if stages else "",
            "funnel_stages": [event["event_type"] for event in sorted(stages, key=lambda event: event["sequence"])],
            "plan_ids": plan_ids,
            "trade_ids": trade_ids,
            "outcome_hashes": sorted(str(row.get("outcome_hash")) for row in linked_outcomes if row.get("outcome_hash")),
            "complete": bool(stages and plan_ids and trade_ids and linked_outcomes),
        })

    legacy_forward = sum(not event.get("candidate_id") for event in forward_events)
    legacy_outcomes = sum(not row.get("candidate_id") for row in outcomes)
    payload = {
        "schema_version": 2,
        "link_policy": "candidate_id_only",
        "structured_events_preferred": True,
        "records": records,
        "legacy": {
            "forward_events_unlinked": legacy_forward,
            "outcomes_unlinked": legacy_outcomes,
            "status": "LEGACY_UNLINKED" if legacy_forward or legacy_outcomes else "NONE",
        },
    }
    payload["reconstruction_hash"] = _hash(payload)
    return payload
