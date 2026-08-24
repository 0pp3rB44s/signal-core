"""adaptive_trend_tsmom_v1 -- live signal evaluation and shadow-mode wiring.

This module wires the already-tested pure-logic modules (adaptive_trend_tsmom,
adaptive_trend_candles, adaptive_trend_shadow) to the real Bitget market feed
and produces the four required structured log events for every evaluated
symbol on every new closed 6H candle:

    ADAPTIVE_6H_CANDLE_CLOSED
    ADAPTIVE_SIGNAL_EVALUATED
    ADAPTIVE_SIGNAL_SELECTED
    ADAPTIVE_SIGNAL_REJECTED

Scope boundary, deliberate: this module NEVER submits a Bitget order, places
protection, or creates a durable execution intent. Every actionable outcome
--frozen or not-- is written to the shadow decision log. Real order
submission (Phase 3+ of the AdaptiveTrend execution rollout) is a separate,
not-yet-built integration; until that exists, routing every decision through
shadow mode is what makes this module safe to run against the live feed
regardless of account/risk state.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from strategies.adaptive_trend_candles import (
    check_data_health,
    closed_only,
    unprocessed_since,
)
from strategies.adaptive_trend_shadow import (
    ShadowDecisionLog,
    build_freeze_blocked_record,
)
from strategies.adaptive_trend_tsmom import (
    ATR_PERIOD,
    Candle6h,
    MOM_LOOKBACK,
    MOM_THRESHOLD,
    STRATEGY_VERSION,
    Side,
    SignalCandidate,
    classify_signal,
    compute_atr,
    compute_momentum,
    initial_stop,
    rank_candidates,
    size_position,
)

logger = logging.getLogger("adaptive_trend")

CANDLE_LIMIT = MOM_LOOKBACK + ATR_PERIOD + 20  # warmup + safety margin


def fetch_6h_candles(client, symbol: str, product_type: str) -> list[Candle6h]:
    """Native 6H candles straight from Bitget -- no synthetic aggregation.
    Bitget's own API supports 6H granularity directly (see
    clients/bitget_market_client.py's GRANULARITY_MAP), so there is no reason
    to reconstruct it from finer bars and risk a rounding/boundary mismatch
    against the exchange's own candle boundaries."""
    payload = client.get_candles(symbol, product_type, granularity="6h", limit=CANDLE_LIMIT)
    rows = payload.get("data") or []
    out = []
    for row in rows:
        # Bitget candle rows: [ts_ms, open, high, low, close, base_vol, quote_vol]
        try:
            open_ms = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        out.append(Candle6h(open_ms=open_ms, close_ms=open_ms + 6 * 60 * 60 * 1000,
                             open=o, high=h, low=l, close=c))
    out.sort(key=lambda c: c.open_ms)
    return out


@dataclass(slots=True)
class SymbolEvaluation:
    symbol: str
    decision: str            # NO_SIGNAL | SIGNAL_EVALUATED | SIGNAL_SELECTED | SIGNAL_REJECTED | DATA_UNHEALTHY
    candidate: SignalCandidate | None = None
    reason: str = ""
    last_processed_close_ms: int | None = None


def evaluate_symbol(
    *,
    symbol: str,
    raw_candles: list[Candle6h],
    now_ms: int,
    last_processed_close_ms: int | None,
    runtime_sha: str,
) -> SymbolEvaluation:
    """Evaluate exactly the newest unprocessed closed 6H candle for one symbol.

    Every closed candle since `last_processed_close_ms` gets an
    ADAPTIVE_6H_CANDLE_CLOSED log line (so a restart that missed several
    candles doesn't silently lose the record of them having closed), but
    only the LATEST one produces an actionable signal decision -- acting on
    a stale, days-old momentum reading after a restart gap would itself be a
    correctness bug, not a feature.
    """
    closed = closed_only(raw_candles, now_ms=now_ms)
    health = check_data_health(closed, now_ms=now_ms)
    if not health.ok:
        logger.warning("ADAPTIVE_DATA_UNHEALTHY | symbol=%s | reason=%s", symbol, health.reason)
        return SymbolEvaluation(symbol=symbol, decision="DATA_UNHEALTHY", reason=health.reason,
                                 last_processed_close_ms=last_processed_close_ms)

    new_candles = unprocessed_since(closed, last_processed_close_ms=last_processed_close_ms)
    if not new_candles:
        return SymbolEvaluation(symbol=symbol, decision="NO_SIGNAL", reason="no_new_closed_candle",
                                 last_processed_close_ms=last_processed_close_ms)

    for nc in new_candles:
        logger.info(
            "ADAPTIVE_6H_CANDLE_CLOSED | symbol=%s | candle_close_time=%s | close=%s | "
            "strategy_version=%s | runtime_sha=%s",
            symbol, nc.close_ms, nc.close, STRATEGY_VERSION, runtime_sha,
        )

    new_last_processed = new_candles[-1].close_ms
    # `closed` includes every candle up to and including the newest -- slice
    # so momentum/ATR are computed as of exactly that latest closed candle,
    # never anything after it.
    upto_latest = [c for c in closed if c.close_ms <= new_last_processed]

    mom = compute_momentum(upto_latest, lookback=MOM_LOOKBACK)
    atr = compute_atr(upto_latest, period=ATR_PERIOD)
    side = classify_signal(mom, MOM_THRESHOLD)
    close_price = upto_latest[-1].close

    mom_strength = None
    if side is not None and atr is not None and atr > 0:
        mom_strength = abs(mom) / (atr / close_price) if close_price else 0.0

    logger.info(
        "ADAPTIVE_SIGNAL_EVALUATED | symbol=%s | candle_close_time=%s | close=%s | "
        "MOM=%s | ATR=%s | MOM_STRENGTH=%s | decision=%s | strategy_version=%s | runtime_sha=%s",
        symbol, new_last_processed, close_price, mom, atr, mom_strength,
        (side.value if side else "NO_SIGNAL"), STRATEGY_VERSION, runtime_sha,
    )

    if side is None or atr is None or atr <= 0:
        reason = "no_signal" if side is None else "atr_unavailable"
        logger.info(
            "ADAPTIVE_SIGNAL_REJECTED | symbol=%s | candle_close_time=%s | reason=%s | "
            "strategy_version=%s | runtime_sha=%s",
            symbol, new_last_processed, reason, STRATEGY_VERSION, runtime_sha,
        )
        return SymbolEvaluation(symbol=symbol, decision="SIGNAL_REJECTED", reason=reason,
                                 last_processed_close_ms=new_last_processed)

    candidate = SignalCandidate(symbol=symbol, side=side, signal_candle_close_ms=new_last_processed,
                                 close=close_price, mom=mom, atr=atr)
    logger.info(
        "ADAPTIVE_SIGNAL_SELECTED | symbol=%s | side=%s | candle_close_time=%s | close=%s | "
        "MOM=%s | ATR=%s | MOM_STRENGTH=%s | strategy_version=%s | runtime_sha=%s",
        symbol, side.value, new_last_processed, close_price, mom, atr, candidate.mom_strength,
        STRATEGY_VERSION, runtime_sha,
    )
    return SymbolEvaluation(symbol=symbol, decision="SIGNAL_SELECTED", candidate=candidate,
                             last_processed_close_ms=new_last_processed)


def route_selected_candidate(
    *,
    winner: SignalCandidate,
    equity: float,
    exchange_min_notional: float,
    weekly_freeze_active: bool,
    runtime_sha: str,
    shadow_log: ShadowDecisionLog,
) -> str:
    """Route the one selected (ranked-winner) candidate.

    Deliberate scope boundary: this always ends in a shadow record, never a
    real order -- see module docstring. `weekly_freeze_active` only changes
    the record's rejection_reason for observability; it does not gate
    whether a shadow record is written, since a shadow record must exist for
    every evaluated actionable signal regardless of freeze state.
    """
    side = winner.side
    stop = initial_stop(winner.close, winner.atr, side)
    sizing = size_position(equity=equity, entry_price=winner.close, stop_price=stop,
                            exchange_min_notional=exchange_min_notional)

    record = build_freeze_blocked_record(
        timestamp=str(int(time.time() * 1000)), symbol=winner.symbol, side=side.value,
        six_h_close=winner.close, mom=winner.mom, atr=winner.atr,
        mom_strength=winner.mom_strength, entry_candidate=winner.close, initial_stop=stop,
        risk_pct=sizing.effective_risk_pct if sizing.accepted else None,
        notional=sizing.rounded_notional if sizing.accepted else None,
        code_sha=runtime_sha,
    )
    if not sizing.accepted:
        record["rejection_reason"] = sizing.rejection_reason
    elif not weekly_freeze_active:
        # Sizing succeeded and the account is not frozen -- this is exactly
        # the candidate a real order path would act on. It is still routed
        # to shadow only, because that path does not exist yet.
        record["rejection_reason"] = "order_submission_not_yet_implemented"

    shadow_log.append(record)
    logger.warning(
        "ADAPTIVE_SHADOW_RECORDED | symbol=%s | side=%s | decision=%s | reason=%s",
        winner.symbol, side.value, record["decision"], record["rejection_reason"],
    )
    return record["rejection_reason"]


def evaluate_universe(
    *,
    client,
    product_type: str,
    symbols: tuple[str, ...],
    now_ms: int,
    last_processed: dict[str, int | None],
    equity: float,
    exchange_min_notional: dict[str, float],
    weekly_freeze_active: bool,
    runtime_sha: str,
    shadow_log: ShadowDecisionLog,
) -> dict:
    """One evaluation pass across the whole symbol universe.

    Returns the updated `last_processed` map (caller persists it via the
    existing JsonStateStore pattern -- this function does no I/O of its own
    for that state, matching the pure-logic modules it wraps) plus a summary
    of what happened, for logging/observability at the call site.
    """
    evaluations: list[SymbolEvaluation] = []
    for symbol in symbols:
        try:
            candles = fetch_6h_candles(client, symbol, product_type)
        except Exception:
            logger.exception("ADAPTIVE_CANDLE_FETCH_FAILED | symbol=%s", symbol)
            evaluations.append(SymbolEvaluation(symbol=symbol, decision="DATA_UNHEALTHY",
                                                  reason="fetch_failed",
                                                  last_processed_close_ms=last_processed.get(symbol)))
            continue
        ev = evaluate_symbol(symbol=symbol, raw_candles=candles, now_ms=now_ms,
                              last_processed_close_ms=last_processed.get(symbol),
                              runtime_sha=runtime_sha)
        evaluations.append(ev)
        if ev.last_processed_close_ms is not None:
            last_processed[symbol] = ev.last_processed_close_ms

    candidates = [ev.candidate for ev in evaluations if ev.candidate is not None]
    winner = rank_candidates(candidates)
    routed_reason = None
    if winner is not None:
        routed_reason = route_selected_candidate(
            winner=winner, equity=equity,
            exchange_min_notional=exchange_min_notional.get(winner.symbol, 5.0),
            weekly_freeze_active=weekly_freeze_active, runtime_sha=runtime_sha,
            shadow_log=shadow_log,
        )

    return dict(
        last_processed=last_processed,
        evaluations=evaluations,
        winner_symbol=winner.symbol if winner else None,
        routed_reason=routed_reason,
    )
