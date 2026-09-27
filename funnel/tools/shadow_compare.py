"""Prospective ORIGINAL-vs-TUNED shadow: runs both variants side by side on identical market data (via
funnel.tools.tuning_round1_ab, the same harness Round 1's historical-style validation used) and logs every
DIVERGENCE - a step where one variant accepted a candidate and the other did not, or sized it differently -
with timestamp, symbol, strategy, baseline decision, tuned decision, reason, simulated outcome. No real
orders; SimExchange only. The two variants never share state (separate engines, separate SimExchange
instances, separate RiskManager instances) - the only path between them is the open-positions file each
overwrites with only ITS OWN positions immediately before its own decision (see tuning_round1_ab.py), so
they cannot interfere with one another's decisions.

This module is meant to be re-run repeatedly (e.g. on a schedule, against freshly-fetched data each time) so
that divergences accumulate with genuine elapsed time - the actual sealed-holdout substitute this mission's
data-separation section calls for when no untouched historical holdout exists (see
TUNING_ROUND1_VALIDATION_2026-09-27.md). A single run's divergence log is a snapshot, not the holdout
itself; the holdout is what accumulates across repeated runs over time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from funnel.tools.tuning_round1_ab import TUNED_MAX_CLUSTER_EXPOSURE_PCT, run


def divergences(variant_a, variant_b) -> list[dict[str, Any]]:
    a_accept = {(x["symbol"], x["strategy"], x["t_ms"]): x for x in variant_a.accepted}
    b_accept = {(x["symbol"], x["strategy"], x["t_ms"]): x for x in variant_b.accepted}
    a_signal_keys = {(s["symbol"], s["strategy"], s["t_ms"]) for s in variant_a.signals}
    b_signal_keys = {(s["symbol"], s["strategy"], s["t_ms"]) for s in variant_b.signals}
    all_keys = a_signal_keys | b_signal_keys
    out: list[dict[str, Any]] = []
    for symbol, strategy, t_ms in sorted(all_keys, key=lambda k: k[2]):
        key = (symbol, strategy, t_ms)
        a_took = key in a_accept
        b_took = key in b_accept
        if a_took == b_took and (not a_took or a_accept[key]["notional"] == b_accept[key]["notional"]):
            continue
        out.append({
            "timestamp_utc": datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat(),
            "t_ms": t_ms, "symbol": symbol, "strategy": strategy,
            "baseline_decision": "ACCEPT" if a_took else "REJECT",
            "tuned_decision": "ACCEPT" if b_took else "REJECT",
            "baseline_notional": a_accept.get(key, {}).get("notional"),
            "tuned_notional": b_accept.get(key, {}).get("notional"),
            "reason": "tuned cluster-exposure cap (40% vs 120%) changed the outcome" if a_took != b_took
                      else "tuned cluster-exposure cap changed the sized notional",
            "simulated_outcome_baseline": "see variant_a.trades for this setup_id (SimExchange only, no real order)",
        })
    return out


def main() -> None:
    result = run(nav_usdt=100.0)
    log = divergences(result["variant_a"], result["variant_b"])
    print(json.dumps({
        "TUNED_MAX_CLUSTER_EXPOSURE_PCT": TUNED_MAX_CLUSTER_EXPOSURE_PCT,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "steps_compared": result["timeline_length"],
        "divergence_count": len(log),
        "divergences": log,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
