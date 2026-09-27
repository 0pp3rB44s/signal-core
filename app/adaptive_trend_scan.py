"""Wires adaptive_trend_tsmom_v1's already-tested signal-evaluation pass
(strategies/adaptive_trend_runtime.evaluate_universe) into the live scan
cycle, in shadow mode only.

Deliberate scope boundary, matching every other AdaptiveTrend module in this
rollout: this file never calls ExecutionService.execute() and never submits
an order. It only produces shadow decision records (data_store/adaptive_trend/
shadow_decisions.jsonl) for forward observability, per the phased rollout
plan -- entry stays shadow-only until the owner explicitly wires live order
eligibility.

State is a single JSON file mapping symbol -> last_processed_close_ms,
persisted via the existing JsonStateStore pattern. evaluate_universe() does
no I/O of its own for that state (by design); this module owns reading and
writing it, exactly like the caller contract in adaptive_trend_candles.py
describes.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.equity import resolve_account_equity
from execution.state_store import JsonStateStore
from strategies.adaptive_trend_runtime import evaluate_universe
from strategies.adaptive_trend_shadow import ShadowDecisionLog
from strategies.adaptive_trend_tsmom import SYMBOL_UNIVERSE

logger = logging.getLogger("AdaptiveTrendScan")

SCAN_STATE_PATH = "state/adaptive_trend_scan_state.json"
DEPLOYED_COMMIT_PATH = "state/deployed_commit.txt"
# Conservative shadow-sizing default; real per-symbol exchange minimums are
# irrelevant here since no order is ever submitted from this path.
_DEFAULT_MIN_NOTIONAL = 5.0


def _runtime_sha() -> str:
    try:
        return Path(DEPLOYED_COMMIT_PATH).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def run_adaptive_trend_shadow_scan(
    *, client, settings, weekly_freeze_active: bool,
    state_store: JsonStateStore | None = None,
    shadow_log: ShadowDecisionLog | None = None,
) -> dict:
    """One shadow-only evaluation pass across SYMBOL_UNIVERSE.

    Never raises -- callers (the main scan cycle) must not have their loop
    killed by a data/network hiccup here, matching every other independent
    block in _scan_cycle. Returns a summary dict for logging; on failure
    returns {"error": ...} instead of propagating.
    """
    store = state_store or JsonStateStore(SCAN_STATE_PATH)
    log = shadow_log or ShadowDecisionLog()
    try:
        last_processed = dict(store.load(default={}))
        equity, _source = resolve_account_equity(settings)
        result = evaluate_universe(
            client=client,
            product_type=settings.bitget_product_type,
            symbols=SYMBOL_UNIVERSE,
            now_ms=int(time.time() * 1000),
            last_processed=last_processed,
            equity=equity,
            exchange_min_notional={s: _DEFAULT_MIN_NOTIONAL for s in SYMBOL_UNIVERSE},
            weekly_freeze_active=weekly_freeze_active,
            runtime_sha=_runtime_sha(),
            shadow_log=log,
        )
        store.save(result["last_processed"])
        logger.info(
            "ADAPTIVE_TREND_SHADOW_SCAN_DONE | winner=%s | routed_reason=%s | evaluated=%s",
            result.get("winner_symbol"), result.get("routed_reason"),
            len(result.get("evaluations") or []),
        )
        return result
    except Exception as exc:
        logger.exception("ADAPTIVE_TREND_SHADOW_SCAN_FAILED | error=%s", exc)
        return {"error": str(exc)}
