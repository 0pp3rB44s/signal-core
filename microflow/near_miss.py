"""Near-miss telemetry: why a MicroFlow episode did or did not become a candidate.

The 2026-08-16 AVAX forensics proved a structural interaction between two gates:
the volatility floor needs the move to have already happened, while the persistence
requirement needs the microstructure to have settled down again. Together they can
only open after a move completes. What is *not* established is whether that costs
money, and nothing here changes behaviour — this module exists to make the question
answerable from data instead of from one anecdote.

Two rules shape the design:

* **One source of truth for the gates.** `evaluate_gates` is what the sampler's own
  decision is built from, so telemetry and execution cannot drift apart. A gate that
  is reported as passing *is* the gate that passed.
* **Episodes, not frames.** The collector sees a few snapshots per second per symbol.
  Emitting a row each time would dwarf the trade data it exists to capture, so rows
  are written on state transitions only, with a bounded heartbeat while an episode is
  open.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "microflow_near_miss_v1"

#: How long an episode may stay open without a transition before a heartbeat row is
#: written. Keeps a long alignment visible without emitting per-frame noise.
HEARTBEAT_MS = 5_000

#: Price history kept per symbol for the prior-move context. 5 minutes at a few
#: snapshots per second, capped so memory cannot grow with uptime.
PRICE_HISTORY_MS = 300_000
#: 5 minutes at the collector's observed rate, not an arbitrary power of two: the
#: longest prior-move window is 5 min, so anything older is dead weight per traversal.
PRICE_HISTORY_MAX = 1_536

#: Windows reported as prior move, in milliseconds.
PRIOR_MOVE_WINDOWS = (("1s", 1_000), ("5s", 5_000), ("15s", 15_000),
                      ("30s", 30_000), ("60s", 60_000), ("2m", 120_000), ("5m", 300_000))

#: Persistence milestones at which the volatility reading is captured, so it can be
#: shown whether range rises *while* an alignment matures — the AVAX mechanism.
PERSISTENCE_MILESTONES_MS = (500, 1_000, 1_500, 2_000)


class EpisodeState:
    PRE_SIGNAL = "PRE_SIGNAL"
    NEAR_CANDIDATE = "NEAR_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class ResetCause:
    OFI_FLIP = "OFI_FLIP"
    BOOK_FLIP = "BOOK_FLIP"
    MICROPRICE_FLIP = "MICROPRICE_FLIP"
    FRESHNESS = "FRESHNESS"
    VOLATILITY_FLOOR = "VOLATILITY_FLOOR"
    SPREAD = "SPREAD"
    COOLDOWN = "COOLDOWN"
    DIRECTION_CHANGE = "DIRECTION_CHANGE"
    OTHER = "OTHER"


@dataclass(frozen=True)
class GateEvaluation:
    """Every gate's value, threshold and verdict for one snapshot.

    ``direction`` is exactly what the sampler acts on. ``last_failed_gate`` is the
    first gate that failed in evaluation order, which is the one worth reporting:
    downstream gates were never reached.
    """

    direction: str | None
    intent: str | None            # direction the flow *leans*, even when a gate fails
    values: dict[str, Any]
    passes: dict[str, bool]
    distance_to_pass: dict[str, float]
    last_failed_gate: str | None

    @property
    def all_pass(self) -> bool:
        return self.direction is not None


def _sign(value: float | None, threshold: float = 0.0) -> int:
    if value is None:
        return 0
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def evaluate_gates(snapshot: dict, spec) -> GateEvaluation:
    """Evaluate every MicroFlow entry gate for one snapshot.

    Ordered exactly as the sampler evaluates them, so ``last_failed_gate`` names the
    gate that actually stopped the snapshot rather than an arbitrary later one.
    """
    flow = (snapshot.get("trade_flow") or {}).get(spec.ofi_window) or {}
    flow60 = (snapshot.get("trade_flow") or {}).get("60s") or {}
    book = snapshot.get("book") or {}
    micro = snapshot.get("microprice") or {}
    fresh = snapshot.get("freshness") or {}

    ofi = flow.get("ofi") if isinstance(flow, dict) else flow
    imbalance = book.get("book_imbalance_top5")
    edge = micro.get("microprice_vs_mid_bps")
    spread = book.get("spread_bps")
    movement = flow60.get("realized_range_bps") if isinstance(flow60, dict) else None
    trade_age = fresh.get("trade_stream_age_ms")
    book_age = fresh.get("book_stream_age_ms")

    values = {
        "ofi": ofi, "imbalance_top5": imbalance, "imbalance_top1": book.get("book_imbalance_top1"),
        "microprice_edge_bps": edge, "spread_bps": spread,
        "realized_range_60s_bps": movement,
        "trade_stream_age_ms": trade_age, "book_stream_age_ms": book_age,
        "sequence_valid": fresh.get("sequence_valid"),
        "mid_price": micro.get("mid_price"),
        "ofi_1s": ((snapshot.get("trade_flow") or {}).get("1s") or {}).get("ofi"),
        "ofi_5s": ((snapshot.get("trade_flow") or {}).get("5s") or {}).get("ofi"),
        "ofi_15s": ((snapshot.get("trade_flow") or {}).get("15s") or {}).get("ofi"),
        "ofi_30s": ((snapshot.get("trade_flow") or {}).get("30s") or {}).get("ofi"),
        "ofi_60s": ((snapshot.get("trade_flow") or {}).get("60s") or {}).get("ofi"),
    }

    fresh_ok = (
        fresh.get("sequence_valid") is True
        and isinstance(trade_age, (int, float)) and isinstance(book_age, (int, float))
        and trade_age <= spec.freshness_ms and book_age <= spec.freshness_ms
    )
    spread_ok = isinstance(spread, (int, float)) and spread <= spec.max_spread_bps
    vol_ok = isinstance(movement, (int, float)) and movement >= spec.minimum_60s_range_bps
    inputs_ok = ofi is not None and imbalance is not None and edge is not None

    # The direction the flow leans, regardless of whether the gates allow acting on
    # it. Without this a blocked episode has no side and cannot be labelled later.
    s_ofi, s_book, s_edge = _sign(ofi, spec.ofi_threshold), _sign(imbalance, spec.book_threshold), _sign(edge, spec.microprice_edge_bps)
    intent = None
    if s_ofi and s_ofi == s_book == s_edge:
        intent = "LONG" if s_ofi > 0 else "SHORT"

    ofi_ok = inputs_ok and abs(ofi) >= spec.ofi_threshold
    imb_ok = inputs_ok and abs(imbalance) >= spec.book_threshold
    micro_ok = inputs_ok and abs(edge) > spec.microprice_edge_bps
    aligned = intent is not None

    passes = {
        "freshness": bool(fresh_ok), "spread": bool(spread_ok), "volatility_floor": bool(vol_ok),
        "inputs_present": bool(inputs_ok), "ofi": bool(ofi_ok), "imbalance": bool(imb_ok),
        "microprice": bool(micro_ok), "alignment": bool(aligned),
    }

    def gap(value, threshold, *, upper=False):
        if not isinstance(value, (int, float)):
            return float("nan")
        return (threshold - value) if not upper else (value - threshold)

    distance = {
        "volatility_floor": gap(movement, spec.minimum_60s_range_bps),
        "ofi": gap(abs(ofi) if isinstance(ofi, (int, float)) else None, spec.ofi_threshold),
        "imbalance": gap(abs(imbalance) if isinstance(imbalance, (int, float)) else None, spec.book_threshold),
        "microprice": gap(abs(edge) if isinstance(edge, (int, float)) else None, spec.microprice_edge_bps),
        "spread": gap(spread, spec.max_spread_bps, upper=True),
    }

    last_failed = None
    for name in ("freshness", "spread", "volatility_floor", "inputs_present",
                 "ofi", "imbalance", "microprice", "alignment"):
        if not passes[name]:
            last_failed = name
            break

    direction = intent if (fresh_ok and spread_ok and vol_ok and inputs_ok and aligned) else None
    return GateEvaluation(direction=direction, intent=intent, values=values,
                          passes=passes, distance_to_pass=distance, last_failed_gate=last_failed)


@dataclass
class _Episode:
    episode_id: str
    symbol: str
    side: str
    started_ms: int
    state: str = EpisodeState.PRE_SIGNAL
    aligned_since_ms: int | None = None
    max_alignment_ms: int = 0
    resets: int = 0
    reset_causes: dict[str, int] = field(default_factory=dict)
    reset_log: list[dict] = field(default_factory=list)
    range_at_first_alignment: float | None = None
    range_at_milestone: dict[str, float] = field(default_factory=dict)
    last_emit_ms: int = 0
    last_seen_ms: int = 0
    start_context: dict = field(default_factory=dict)


class NearMissTracker:
    """One directional episode per symbol, emitted on transitions only.

    This is observation, never control: the tracker is fed the same
    :class:`GateEvaluation` the sampler decides on, and returns rows for a writer. It
    holds no reference to execution and can be removed without changing behaviour.
    """

    #: A reset log that grew without bound would be a slow memory leak on a process
    #: that runs for days. The first resets are the informative ones.
    MAX_RESET_LOG = 40

    def __init__(self, spec, *, heartbeat_ms: int = HEARTBEAT_MS) -> None:
        self.spec = spec
        self.heartbeat_ms = heartbeat_ms
        self._episodes: dict[str, _Episode] = {}
        self._prices: dict[str, deque] = {}

    # --- price context -----------------------------------------------------

    def _record_price(self, symbol: str, now_ms: int, mid: float | None) -> None:
        if not isinstance(mid, (int, float)) or mid <= 0:
            return
        hist = self._prices.setdefault(symbol, deque(maxlen=PRICE_HISTORY_MAX))
        hist.append((now_ms, float(mid)))
        cutoff = now_ms - PRICE_HISTORY_MS
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def _prior_moves(self, symbol: str, now_ms: int, mid: float | None) -> dict[str, float | None]:
        """Prior returns and range position, in a single pass over the history.

        Called only when a row is emitted, and deliberately one traversal rather than
        one per window: the deque holds thousands of points and this runs inside the
        collector's hot path.
        """
        empty = {f"prior_move_{label}_bps": None for label, _ in PRIOR_MOVE_WINDOWS}
        hist = self._prices.get(symbol)
        if not hist or not isinstance(mid, (int, float)) or mid <= 0:
            return {**empty, "distance_from_local_high_bps": None,
                    "distance_from_local_low_bps": None, "range_position": None}

        targets = [(f"prior_move_{label}_bps", now_ms - window) for label, window in PRIOR_MOVE_WINDOWS]
        found: dict[str, float] = {}
        lo = hi = None
        for ts, price in hist:                      # oldest first, single traversal
            if lo is None or price < lo:
                lo = price
            if hi is None or price > hi:
                hi = price
            for key, cutoff in targets:
                if key not in found and ts >= cutoff:
                    found[key] = price
        out: dict[str, float | None] = {
            key: (round((mid - found[key]) / found[key] * 10_000.0, 4) if key in found else None)
            for key, _ in targets
        }
        out["distance_from_local_high_bps"] = round((mid - hi) / hi * 10_000.0, 4) if hi else None
        out["distance_from_local_low_bps"] = round((mid - lo) / lo * 10_000.0, 4) if lo else None
        out["range_position"] = round((mid - lo) / (hi - lo), 4) if (hi and lo and hi > lo) else None
        return out

    # --- episode lifecycle -------------------------------------------------

    @staticmethod
    def _episode_id(symbol: str, side: str, started_ms: int) -> str:
        return hashlib.sha256(f"{symbol}|{side}|{started_ms}".encode()).hexdigest()[:32]

    def _row(self, ep: _Episode, ev: GateEvaluation, now_ms: int, state: str,
             *, reason: str | None = None, context: dict | None = None) -> dict:
        mid = ev.values.get("mid_price")
        # `is not None`, not truthiness: an alignment that began at timestamp 0 is
        # real, and treating it as absent silently reports zero persistence.
        alignment_ms = (now_ms - ep.aligned_since_ms) if ep.aligned_since_ms is not None else 0
        return {
            "schema_version": SCHEMA_VERSION,
            "episode_id": ep.episode_id,
            "symbol": ep.symbol,
            "side": ep.side,
            "state": state,
            "reason": reason,
            "timestamp_start_ms": ep.started_ms,
            "timestamp_ms": now_ms,
            "episode_age_ms": now_ms - ep.started_ms,
            "alignment_ms": alignment_ms,
            "max_alignment_ms": max(ep.max_alignment_ms, alignment_ms),
            "resets": ep.resets,
            "reset_causes": dict(ep.reset_causes),
            "reset_log": list(ep.reset_log),
            "range_at_first_alignment_bps": ep.range_at_first_alignment,
            "range_at_milestone_bps": dict(ep.range_at_milestone),
            "last_failed_gate": ev.last_failed_gate,
            "distance_to_pass": {k: (None if v != v else round(v, 6))
                                 for k, v in ev.distance_to_pass.items()},
            "gates": dict(ev.passes),
            "values": dict(ev.values),
            "start_context": dict(ep.start_context),
            "context": context if context is not None else self._prior_moves(ep.symbol, now_ms, mid),
            "spec_id": self.spec.spec_id,
            "research_only": True,
        }

    def observe(self, snapshot: dict, ev: GateEvaluation, *,
                candidate_id: str | None = None) -> list[dict]:
        """Feed one snapshot. Returns rows to persist — usually none."""
        symbol = str(snapshot.get("symbol") or "")
        raw_ts = snapshot.get("timestamp_local")
        # Guard on absence, not falsiness: timestamp 0 is a valid instant and
        # rejecting it silently drops the first snapshot of any replayed episode.
        if not symbol or not isinstance(raw_ts, (int, float)):
            return []
        now_ms = int(raw_ts)
        mid = ev.values.get("mid_price")
        self._record_price(symbol, now_ms, mid)

        # One traversal per snapshot, shared by every row this call emits.
        context: dict | None = None
        def ctx() -> dict:
            nonlocal context
            if context is None:
                context = self._prior_moves(symbol, now_ms, mid)
            return context

        rows: list[dict] = []
        ep = self._episodes.get(symbol)
        intent = ev.intent

        # No lean and no open episode: nothing to say.
        if intent is None and ep is None:
            return rows

        if ep is not None and intent is not None and intent != ep.side:
            rows.append(self._row(ep, ev, now_ms, EpisodeState.EXPIRED,
                                  reason=ResetCause.DIRECTION_CHANGE))
            self._episodes.pop(symbol, None)
            ep = None

        if ep is None:
            if intent is None:
                return rows
            ep = _Episode(episode_id=self._episode_id(symbol, intent, now_ms),
                          symbol=symbol, side=intent, started_ms=now_ms,
                          start_context=ctx())
            self._episodes[symbol] = ep
            rows.append(self._row(ep, ev, now_ms, EpisodeState.PRE_SIGNAL, reason="episode_open", context=ctx()))

        ep.last_seen_ms = now_ms

        if ev.all_pass:
            if ep.aligned_since_ms is None:
                ep.aligned_since_ms = now_ms
                ep.range_at_first_alignment = ev.values.get("realized_range_60s_bps")
                if ep.state != EpisodeState.NEAR_CANDIDATE:
                    ep.state = EpisodeState.NEAR_CANDIDATE
                    rows.append(self._row(ep, ev, now_ms, EpisodeState.NEAR_CANDIDATE,
                                          reason="alignment_started"))
            alignment_ms = now_ms - ep.aligned_since_ms
            ep.max_alignment_ms = max(ep.max_alignment_ms, alignment_ms)
            for ms in PERSISTENCE_MILESTONES_MS:
                key = f"{ms}ms"
                if alignment_ms >= ms and key not in ep.range_at_milestone:
                    ep.range_at_milestone[key] = ev.values.get("realized_range_60s_bps")
            if candidate_id is not None:
                row = self._row(ep, ev, now_ms, EpisodeState.CANDIDATE, reason="candidate_emitted", context=ctx())
                row["candidate_id"] = candidate_id
                rows.append(row)
                self._episodes.pop(symbol, None)
                return rows
        else:
            if ep.aligned_since_ms is not None:
                cause = {
                    "freshness": ResetCause.FRESHNESS,
                    "spread": ResetCause.SPREAD,
                    "volatility_floor": ResetCause.VOLATILITY_FLOOR,
                    "inputs_present": ResetCause.OTHER,
                    "ofi": ResetCause.OFI_FLIP,
                    "imbalance": ResetCause.BOOK_FLIP,
                    "microprice": ResetCause.MICROPRICE_FLIP,
                    "alignment": ResetCause.OTHER,
                }.get(ev.last_failed_gate or "", ResetCause.OTHER)
                held = now_ms - ep.aligned_since_ms
                ep.resets += 1
                ep.reset_causes[cause] = ep.reset_causes.get(cause, 0) + 1
                if len(ep.reset_log) < self.MAX_RESET_LOG:
                    ep.reset_log.append({"timestamp_ms": now_ms, "cause": cause,
                                         "held_ms": held,
                                         "range_bps": ev.values.get("realized_range_60s_bps")})
                ep.aligned_since_ms = None
                rows.append(self._row(ep, ev, now_ms, EpisodeState.BLOCKED, reason=cause, context=ctx()))

        if not rows and now_ms - ep.last_emit_ms >= self.heartbeat_ms:
            rows.append(self._row(ep, ev, now_ms, ep.state, reason="heartbeat", context=ctx()))

        if rows:
            ep.last_emit_ms = now_ms
        return rows

    def expire_stale(self, now_ms: int, *, max_idle_ms: int = 60_000) -> list[dict]:
        """Close episodes that stopped receiving snapshots, so state cannot leak."""
        rows = []
        for symbol, ep in list(self._episodes.items()):
            if now_ms - ep.last_seen_ms >= max_idle_ms:
                empty = GateEvaluation(direction=None, intent=None, values={}, passes={},
                                       distance_to_pass={}, last_failed_gate=None)
                rows.append(self._row(ep, empty, now_ms, EpisodeState.EXPIRED, reason="idle"))
                self._episodes.pop(symbol, None)
        return rows
