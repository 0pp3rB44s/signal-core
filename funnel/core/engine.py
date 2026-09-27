"""FunnelEngine: SIGNAL_CORE_FUNNEL_V1 restored onto the current hardened execution/protection architecture.

This is the ONE new implementation of the funnel's DECISION ORCHESTRATION (which detector to call, in
what order, how to score/select/plan). The actual decision MATH is not reimplemented here: it is called,
unmodified, from the vendored/frozen strategy modules (see the module-level imports below and
vendor_frozen_5a28980d/README.md for exactly which files are frozen-at-commit vs. current-shared-infra).

Structurally this mirrors lsrl/core/engine.py::LSRLEngine on purpose: same public surface
(cfg, on_event, halt, snapshot, restore, begin_session), so it is a drop-in engine for the SAME
strategy-agnostic plumbing LSRL_V1 already uses (FeedPipeline, SimDriver, SimExchange, LSRLRuntime,
CandleFeed, BitgetWsFeed, CentralRiskGate) - nothing in that plumbing is strategy-specific, and nothing
in it is modified for this restoration. Emits ONLY the four generic intents: PlaceEntry, CancelOrder,
SetStop, SetTargets - the same no-bypass vocabulary lsrl/adapters/strategy_port.py enforces.

Disclosed, deliberate fidelity gap (see AGENT_A_RESTORATION_2026-09-27.md for the full disclosure):
the original system's MarketSnapshot notes carried orderbook-imbalance / liquidity-heatmap / breakout-
pressure telemetry from a "unified feature engine" and a live orderbook archiver that this restoration
does not reconstruct. Candle-derived fields (EMA20/50, trend, range_pct, change_pct, volume_ratio_20)
are computed here with standard formulas; microstructure notes are simply left absent. Every strategy's
own note-extraction helpers already default safely when a marker is absent (fail toward "no evidence",
never fabricated evidence) - this restoration relies on that existing fail-safe behavior rather than
inventing orderbook data that was never observed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.equity import write_equity_snapshot
from clients.schemas import Candle, ContractSpec, MarketSnapshot, StrategyCandidate, TimeframeSnapshot
from lsrl.core.gates import RiskDecision, RiskGate, RiskProposal
from lsrl.core.types import (BarClosed, CancelOrder, DepthSnapshot, Direction, FeedHealth, FillEvent,
                             Intent, NavEvent, OrderEvent, PlaceEntry, Record, SetStop, SetTargets,
                             SymbolRules, TargetOrder, TimerTick, TradeTick)

from funnel.core.config import STRATEGY_ID, FunnelConfig

# Frozen-at-5a28980d strategy decision code (see vendor_frozen_5a28980d/README.md)
from vendor_frozen_5a28980d.planning.trade_planner import TradePlanner
from vendor_frozen_5a28980d.strategies.strategies.low_vol_reclaim import detect_low_vol_reclaim

# Byte-identical between the frozen commit and this monorepo snapshot (verified at restoration time;
# see AGENT_A_RESTORATION_2026-09-27.md) - imported from the shared path, not duplicated.
# MomentumBreakdownStrategy is a SUBCLASS of MomentumBreakoutStrategy (strategies/momentum_breakout.py:839),
# not a separate detection path re-derived from one call - the original runner instantiates and calls both
# separately (app/runner.py:474-475, 1190-1191), and this restoration now does the same (corrected in
# Phase 2 from an earlier single-instance approximation; see FUNNEL_MARKET_INPUT_RECONSTRUCTION_2026-09-27.md).
from strategies.momentum_breakout import MomentumBreakoutStrategy, MomentumBreakdownStrategy
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.scoring import StrategyScorer
from strategies.strategies.continuation import detect_continuation
from strategies.strategies.selector import select_best_candidate

EPS = 1e-9
SNAPSHOT_VERSION = 1

# market_features.engine.INTERVAL_MS recognizes exactly these strings (lower-cased); BarClosed.tf_s stays
# the canonical LSRL-style seconds field on the wire, this is only the label the vendored strategy code needs.
_GRANULARITY_LABEL = {900: "15m", 3600: "1h"}


def _cid(symbol: str, bar_open_ts: int) -> str:
    return hashlib.sha256(f"{STRATEGY_ID}|{symbol}|{bar_open_ts}".encode()).hexdigest()[:12]


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def _trend(ema20: float, ema50: float) -> str:
    if ema20 > ema50:
        return "bullish"
    if ema20 < ema50:
        return "bearish"
    return "mixed"


def _alignment(primary_trend: str, confirmation_trend: str) -> str:
    if primary_trend == confirmation_trend == "bullish":
        return "aligned_bullish"
    if primary_trend == confirmation_trend == "bearish":
        return "aligned_bearish"
    return "mixed"


def build_timeframe_snapshot(symbol: str, granularity: str, candles: list[Candle], t_ms: int) -> TimeframeSnapshot:
    """Standard-formula technical fields from candle history only (no orderbook/microstructure input).
    Disclosed fidelity gap: see funnel/core/engine.py module docstring."""
    closes = [c.close for c in candles]
    latest = candles[-1]
    ema20 = _ema(closes[-20:] if len(closes) >= 20 else closes, min(20, len(closes)) or 1)
    ema50 = _ema(closes[-50:] if len(closes) >= 50 else closes, min(50, len(closes)) or 1)
    change_pct = ((latest.close - latest.open) / latest.open * 100.0) if latest.open else 0.0
    range_pct = ((latest.high - latest.low) / latest.close * 100.0) if latest.close else 0.0
    window = candles[-20:] if len(candles) >= 20 else candles
    avg_vol = (sum(c.volume_base for c in window) / len(window)) if window else 0.0
    volume_ratio_20 = (latest.volume_base / avg_vol) if avg_vol > 0 else 0.0
    return TimeframeSnapshot(
        symbol=symbol, granularity=granularity, latest_close=latest.close, change_pct=change_pct,
        range_pct=range_pct, volume_ratio_20=volume_ratio_20, ema20=ema20, ema50=ema50,
        trend=_trend(ema20, ema50), candles=list(candles),
        closed_candle_timestamp_ms=latest.timestamp_ms, as_of_timestamp_ms=t_ms,
    )


@dataclass(slots=True)
class FunnelSetup:
    """One open (or pending) funnel position for this engine's symbol. Deliberately simpler than LSRL's
    Setup: the funnel places ONE entry order (no ladder) and a planner-sized stop/target set, not a
    multi-rung reclaim geometry."""
    setup_id: str
    direction: Direction
    strategy: str
    candidate_t_ms: int
    entry_tag: str
    entry_price: float
    entry_qty: float
    stop_price: float
    targets: list[TargetOrder]
    entry_filled_qty: float = 0.0
    remaining_qty: float = 0.0
    state: str = "PENDING_ENTRY"           # PENDING_ENTRY | IN_POSITION | CLOSED
    close_reason: str | None = None
    tp_done: list[str] = field(default_factory=list)


class FunnelEngine:
    """SIGNAL_CORE_FUNNEL_V1 restored: one instance per symbol (see funnel/runtime.py for the multi-symbol
    wiring, which shares ONE RiskManager/CentralRiskGate across all per-symbol instances so the original
    cross-symbol MAX_OPEN_POSITIONS=2 / cluster-exposure caps are enforced globally, exactly as the shared
    RiskManager already does for any other strategy in this monorepo)."""

    def __init__(self, cfg: FunnelConfig, rules: SymbolRules, *, risk_gate: RiskGate, settings: Any,
                 maker_fee_rate: float | None = None, taker_fee_rate: float | None = None,
                 stop_slippage_bps: float | None = None) -> None:
        if rules.symbol != cfg.symbol:
            raise ValueError(f"engine is frozen to {cfg.symbol}, got {rules.symbol}")
        self.cfg = cfg
        self.rules = rules
        self.risk_gate = risk_gate
        self.settings = settings
        self.maker_fee = cfg.assumed_maker_fee_rate if maker_fee_rate is None else maker_fee_rate
        self.taker_fee = cfg.assumed_taker_fee_rate if taker_fee_rate is None else taker_fee_rate
        self.stop_slip_bps = cfg.assumed_stop_slippage_bps if stop_slippage_bps is None else stop_slippage_bps
        self._momentum = MomentumBreakoutStrategy(settings)
        self._momentum_breakdown = MomentumBreakdownStrategy(settings)
        self._sweep = LiquiditySweepStrategy(settings)
        self._scorer = StrategyScorer(settings)
        self._planner = TradePlanner(settings)
        self._primary: list[Candle] = []
        self._confirm: list[Candle] = []
        self.last_snapshot: DepthSnapshot | None = None
        self.nav_usdt: float | None = None
        self.nav_t_ms: int = 0
        self.now_ms: int = 0
        self.halted_reason: str | None = None
        self.signal_allowed: bool = True
        self.feed_epoch: int = 0
        self.setup: FunnelSetup | None = None
        self.seen_setup_ids: list[str] = []
        self._intents: list[Intent] = []
        self._records: list[Record] = []
        self._pending_live_market: MarketSnapshot | None = None

    @property
    def active_setup_id(self) -> str | None:
        return self.setup.setup_id if self.setup else None

    def halt(self, reason: str) -> None:
        self.halted_reason = reason

    def resume(self) -> None:
        self.halted_reason = None

    def begin_session(self, state: dict[str, Any] | None) -> None:
        self._primary, self._confirm = [], []
        self.last_snapshot = None
        self.signal_allowed, self.feed_epoch = True, 0
        self._intents, self._records = [], []
        if state is not None:
            self.restore(state)
            return
        self.setup, self.seen_setup_ids = None, []
        self.nav_usdt, self.nav_t_ms, self.now_ms = None, 0, 0
        self.halted_reason = None

    def on_event(self, ev: Any) -> tuple[list[Intent], list[Record]]:
        self._intents, self._records = [], []
        if isinstance(ev, (DepthSnapshot, TradeTick, BarClosed)) and ev.t_ms < self.now_ms:
            self._record("anomaly", ev.t_ms, {"reason": "OUT_OF_ORDER_MARKET_EVENT", "event_type": ev.TYPE})
            return self._intents, self._records
        self.now_ms = max(self.now_ms, ev.t_ms)
        if isinstance(ev, DepthSnapshot):
            if not ev.crossed and ev.bids and ev.asks:
                self.last_snapshot = ev
        elif isinstance(ev, FeedHealth):
            self._on_feed_health(ev)
        elif isinstance(ev, BarClosed):
            self._on_bar(ev)
        elif isinstance(ev, NavEvent):
            self.nav_usdt, self.nav_t_ms = ev.nav_usdt, ev.t_ms
            # app.equity.resolve_account_equity() - which TradePlanner._position_notional() sizes every
            # trade from - reads a separate, file-based equity snapshot (state/account_equity.json) that
            # is otherwise unrelated to this engine's own nav_usdt. Without this, the planner silently
            # falls back to Settings.account_equity_usdt (a fixed placeholder, 1000.0 by default) instead
            # of the NAV this engine is actually simulating against - not a planner change, just wiring
            # this engine into the SAME shared equity-communication channel app/runner.py's own
            # _scan_cycle already writes to (write_equity_snapshot(live_equity)).
            try:
                write_equity_snapshot(float(ev.nav_usdt))
            except Exception:
                pass                                     # never let telemetry plumbing break a decision cycle
        elif isinstance(ev, FillEvent):
            self._on_fill(ev)
        elif isinstance(ev, OrderEvent):
            self._on_order(ev)
        elif isinstance(ev, (TradeTick, TimerTick)):
            pass
        return self._intents, self._records

    def run_live_cycle(self, market: MarketSnapshot, *, t_ms: int, nav_usdt: float) -> tuple[list[Intent], list[Record]]:
        """One real-data decision+fill cycle. `market` must be built by market_features.engine.
        build_market_snapshot (or MarketFetcher.build_market_snapshot) from freshly-fetched Bitget candles/
        order-book - see funnel/tools/live_data_run.py - never a fabricated snapshot. Feeds a NavEvent, then
        a BarClosed synthesized from the real latest-closed primary candle, so this engine's own fill/close
        bookkeeping (unchanged from the synthetic-lifecycle tests) runs on genuine OHLC."""
        self.on_event(NavEvent(t_ms=t_ms, nav_usdt=nav_usdt, source="live"))
        latest = market.primary.candles[-1]
        self._pending_live_market = market
        try:
            bar = BarClosed(t_ms=t_ms, tf_s=self.cfg.primary_tf_s, open_ts_ms=latest.timestamp_ms,
                            close_ts_ms=latest.timestamp_ms + self.cfg.primary_tf_s * 1000,
                            open=latest.open, high=latest.high, low=latest.low, close=latest.close,
                            volume=latest.volume_base)
            return self.on_event(bar)
        finally:
            self._pending_live_market = None

    def _record(self, kind: str, t_ms: int, data: dict[str, Any]) -> None:
        self._records.append(Record(kind, t_ms, data))

    def _on_feed_health(self, ev: FeedHealth) -> None:
        self.signal_allowed = bool(ev.ok)
        self.feed_epoch = ev.epoch
        self._record("feed_health", ev.t_ms, {"ok": ev.ok, "reason": ev.reason, "epoch": ev.epoch})

    # ------------------------------------------------------------------ bars: candle history + decision cycle
    def _on_bar(self, bar: BarClosed) -> None:
        if bar.tf_s == self.cfg.confirmation_tf_s:
            self._confirm.append(Candle(timestamp_ms=bar.open_ts_ms, open=bar.open, high=bar.high,
                                        low=bar.low, close=bar.close, volume_base=bar.volume))
            del self._confirm[:-self.cfg.candle_history_len]
            return
        if bar.tf_s != self.cfg.primary_tf_s:
            return
        self._primary.append(Candle(timestamp_ms=bar.open_ts_ms, open=bar.open, high=bar.high,
                                    low=bar.low, close=bar.close, volume_base=bar.volume))
        del self._primary[:-self.cfg.candle_history_len]
        if self.setup is not None:
            return                                    # one position at a time per symbol; no new detection while in one
        if not self.signal_allowed or self.halted_reason:
            return
        if self._pending_live_market is not None:
            self._run_decision_cycle(bar.t_ms, market=self._pending_live_market)
            return
        if len(self._primary) < 20 or len(self._confirm) < 20:
            return                                     # insufficient history for EMA20 - fail closed, not a fabricated read
        self._run_decision_cycle(bar.t_ms)

    def _market_snapshot(self, t_ms: int) -> MarketSnapshot | None:
        snap = self.last_snapshot
        if snap is None or (t_ms - snap.t_ms) / 1000.0 > self.cfg.max_snapshot_age_s:
            return None
        primary = build_timeframe_snapshot(self.cfg.symbol, _GRANULARITY_LABEL[self.cfg.primary_tf_s],
                                          self._primary, t_ms)
        confirm = build_timeframe_snapshot(self.cfg.symbol, _GRANULARITY_LABEL[self.cfg.confirmation_tf_s],
                                          self._confirm, t_ms)
        contract = ContractSpec(symbol=self.cfg.symbol, product_type="USDT-FUTURES", quote_coin="USDT",
                                base_coin=self.cfg.symbol.removesuffix("USDT"), status="live",
                                min_trade_num=None, size_multiplier=None, price_place=None,
                                volume_24h_usdt=None, change_pct_24h=None, raw={})
        return MarketSnapshot(symbol=self.cfg.symbol, contract=contract, primary=primary, confirmation=confirm,
                              alignment=_alignment(primary.trend, confirm.trend), score_hint=100.0, notes=[])

    def _run_decision_cycle(self, t_ms: int, *, market: MarketSnapshot | None = None) -> None:
        """`market`, when given, is a REAL snapshot built by market_features.engine.build_market_snapshot
        from live-fetched candles/order-book (see funnel/tools/live_data_run.py) - the production feature
        pipeline, unmodified. When omitted (the synthetic-lifecycle test path), it falls back to this
        engine's own simplified candle-only reconstruction from buffered BarClosed events."""
        if market is None:
            market = self._market_snapshot(t_ms)
        if market is None:
            return
        candidate, why = self._detect(market)
        if candidate is None:
            self._record("setup_rejected", t_ms, {"stage": "FUNNEL", "reason": why})
            return
        setup_id = _cid(self.cfg.symbol, candidate.candidate_candle_open_timestamp_ms) + ":" + candidate.strategy
        if setup_id in self.seen_setup_ids:
            self._record("setup_rejected", t_ms, {"stage": "FUNNEL", "reason": "DUPLICATE_CANDIDATE",
                                                  "setup_id": setup_id})
            return
        score = self._scorer.score(candidate)
        if score.verdict != "GO":
            self._record("setup_rejected", t_ms, {"stage": "SCORE", "reason": "SCORE_VERDICT_" + score.verdict,
                                                  "candidate_id": candidate.candidate_id, "score": score.total})
            return
        if self.nav_usdt is None or (t_ms - self.nav_t_ms) / 1000.0 > self.cfg.max_nav_age_s:
            self._record("setup_rejected", t_ms, {"stage": "SIZING", "reason": "NAV_UNAVAILABLE"})
            return
        direction = Direction.LONG if candidate.direction == "LONG" else Direction.SHORT
        entry_hint = candidate.detection.entry_hint
        provisional_notional = min(self.cfg.planner_max_notional_per_trade_usdt, self.nav_usdt * self.cfg.max_leverage)
        try:
            decision = self.risk_gate.check(RiskProposal(
                setup_id=setup_id, symbol=self.cfg.symbol, direction=direction, t_ms=t_ms, nav_usdt=self.nav_usdt,
                entry_prices=(entry_hint,), stop=candidate.detection.invalidation, total_qty=0.0,
                planned_notional_usdt=provisional_notional, planned_loss_usdt=0.0,
                strategy_risk_fraction=self.settings.account_risk_per_trade_pct / 100.0,
                spread_bps=market.primary.range_pct))
        except Exception as exc:                       # fail closed on ANY gate error, exactly like LSRL_V1
            decision = RiskDecision(False, (f"RISK_GATE_ERROR:{type(exc).__name__}",), None, "ERROR")
        if not decision.allowed:
            self.seen_setup_ids.append(setup_id)
            del self.seen_setup_ids[:-1000]
            self._record("setup_rejected", t_ms, {"stage": "RISK_MANAGER", "reason": "RISK_MANAGER_REJECTED",
                                                  "candidate_id": candidate.candidate_id, "reasons": list(decision.reasons)})
            return
        verdict = self._risk_verdict_for_planner(decision, candidate)
        plan = self._planner.build(candidate, score, verdict)
        self.seen_setup_ids.append(setup_id)
        del self.seen_setup_ids[:-1000]
        self._place_from_plan(setup_id, candidate, plan, t_ms)

    def _detect(self, market: MarketSnapshot) -> tuple[StrategyCandidate | None, str]:
        """Same funnel order and same TWO separate strategy instances the last real-money session used
        (app/runner.py:1189-1201: self.strategy / self.momentum_strategy / self.momentum_breakdown_strategy
        / detect_continuation / detect_low_vol_reclaim, in that order), preserved for behavioral fidelity."""
        sweep_candidate = self._sweep.detect(market)
        momentum_candidate = self._momentum.detect(market)
        momentum_breakdown_candidate = self._momentum_breakdown.detect(market)
        continuation_candidate = detect_continuation(market)
        low_vol_reclaim_candidate = detect_low_vol_reclaim(market)
        winner, why = select_best_candidate(
            sweep_candidate=sweep_candidate, continuation_candidate=continuation_candidate,
            low_vol_reclaim_candidate=low_vol_reclaim_candidate, momentum_candidate=momentum_candidate,
            momentum_breakdown_candidate=momentum_breakdown_candidate)
        return winner, why

    def _risk_verdict_for_planner(self, decision: RiskDecision, candidate: StrategyCandidate) -> Any:
        from clients.schemas import RiskVerdict
        applied = decision.applied_risk_fraction
        account_risk_pct = (applied * 100.0) if applied is not None else 0.0
        return RiskVerdict(allowed=True, status="GO", reasons=list(decision.reasons),
                           account_risk_pct=account_risk_pct, leverage=self.cfg.max_leverage,
                           max_open_positions=self.cfg.max_open_positions)

    def _place_from_plan(self, setup_id: str, candidate: StrategyCandidate, plan: Any, t_ms: int) -> None:
        direction = Direction.LONG if plan.direction == "LONG" else Direction.SHORT
        entry_price = plan.entry_prices[0]
        qty = plan.position_notional_usdt / entry_price if entry_price > 0 else 0.0
        if qty <= 0 or plan.position_notional_usdt < self.cfg.planner_min_live_notional_usdt:
            self._record("setup_rejected", t_ms, {"stage": "SIZING", "reason": "BELOW_MIN_NOTIONAL",
                                                  "candidate_id": candidate.candidate_id})
            return
        tag = f"funnel-{setup_id}-e1"
        pcts = plan.tp_size_pcts or [100.0 / max(len(plan.take_profits), 1)] * max(len(plan.take_profits), 1)
        targets = [TargetOrder(tag=f"funnel-{setup_id}-tp{i}", level=f"TP{i}", price=tp,
                              qty=qty * (pct / 100.0))
                   for i, (tp, pct) in enumerate(zip(plan.take_profits, pcts), start=1)]
        self.setup = FunnelSetup(setup_id=setup_id, direction=direction, strategy=candidate.strategy,
                                 candidate_t_ms=t_ms, entry_tag=tag, entry_price=entry_price, entry_qty=qty,
                                 stop_price=plan.stop_loss, targets=targets)
        expires = t_ms + 15 * 60 * 1000                # one primary bar's worth of resting time before re-evaluating
        self._intents.append(PlaceEntry(setup_id=setup_id, tag=tag, rung=1, direction=direction,
                                        price=entry_price, qty=qty, expires_at_ms=expires))
        self._record("setup_accepted", t_ms, {"setup_id": setup_id, "strategy": candidate.strategy,
                                              "candidate_id": candidate.candidate_id, "direction": plan.direction,
                                              "entry": entry_price, "stop": plan.stop_loss,
                                              "take_profits": list(plan.take_profits),
                                              "position_notional_usdt": plan.position_notional_usdt})

    # ------------------------------------------------------------------ order / fills
    def _on_order(self, ev: OrderEvent) -> None:
        s = self.setup
        if s is None or ev.setup_id != s.setup_id:
            self._record("anomaly", ev.t_ms, {"reason": "ORDER_EVENT_FOR_UNKNOWN_SETUP", "setup_id": ev.setup_id})
            return
        if ev.tag == s.entry_tag and ev.status in ("REJECTED", "CANCELLED", "EXPIRED") and s.state == "PENDING_ENTRY":
            self._record("setup_closed", ev.t_ms, {"setup_id": s.setup_id, "reason": "ENTRY_" + ev.status})
            self.setup = None

    def _on_fill(self, f: FillEvent) -> None:
        s = self.setup
        if s is None or f.setup_id != s.setup_id:
            self._record("anomaly", f.t_ms, {"reason": "FILL_FOR_UNKNOWN_SETUP", "setup_id": f.setup_id})
            return
        if f.kind == "ENTRY":
            s.entry_filled_qty += f.qty
            s.remaining_qty = s.entry_filled_qty
            s.state = "IN_POSITION"
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": "ENTRY_FILL",
                                               "price": f.price, "qty": f.qty})
            self._sync_protection(s, "ENTRY_FILL")
            return
        if f.kind in ("TP1", "TP2", "TP3"):
            s.remaining_qty -= f.qty
            if f.kind not in s.tp_done:
                s.tp_done.append(f.kind)
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": f.kind + "_FILL",
                                               "price": f.price, "qty": f.qty})
            if s.remaining_qty <= EPS:
                self._close_setup("TP_FLAT", f.t_ms)
            return
        if f.kind == "SL":
            s.remaining_qty -= f.qty
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": "SL_FILL",
                                               "price": f.price, "qty": f.qty})
            if s.remaining_qty <= EPS:
                self._close_setup("STOP_LOSS", f.t_ms)
            return
        if f.kind == "OTHER_CLOSE":
            s.remaining_qty -= f.qty
            if s.remaining_qty <= EPS:
                self._close_setup("CLOSED_EXTERNALLY", f.t_ms)

    def _sync_protection(self, s: FunnelSetup, reason: str) -> None:
        self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction, price=s.stop_price,
                                     qty=s.remaining_qty, reason=reason))
        if s.targets:
            self._intents.append(SetTargets(setup_id=s.setup_id, direction=s.direction,
                                            targets=tuple(s.targets), reason=reason))

    def _close_setup(self, reason: str, t_ms: int) -> None:
        """Cancel the entry if it never filled, clear the resting stop and cancel any live targets. A flat
        close must leave NOTHING resting on the exchange (cleanup_after_flat: the same check the process-down
        probe verifies for LSRL_V1 applies here - a stop or target order surviving a closed setup is exactly
        the residual that check exists to catch)."""
        s = self.setup
        assert s is not None
        if s.state == "PENDING_ENTRY":
            self._intents.append(CancelOrder(setup_id=s.setup_id, tag=s.entry_tag, reason=reason))
        if s.state == "IN_POSITION":
            self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction, price=s.stop_price,
                                         qty=0.0, reason=reason))
            if s.targets:
                self._intents.append(SetTargets(setup_id=s.setup_id, direction=s.direction, targets=(),
                                                reason=reason))
        s.state, s.close_reason = "CLOSED", reason
        self._record("setup_closed", t_ms, {"setup_id": s.setup_id, "reason": reason, "strategy": s.strategy})
        self.setup = None

    # ------------------------------------------------------------------ persistence
    def snapshot(self) -> dict[str, Any]:
        return {"version": SNAPSHOT_VERSION, "cfg_digest": self.cfg.digest(), "now_ms": self.now_ms,
                "nav": [self.nav_usdt, self.nav_t_ms], "halted_reason": self.halted_reason,
                "seen_setup_ids": list(self.seen_setup_ids),
                "setup": None if self.setup is None else {
                    "setup_id": self.setup.setup_id, "direction": self.setup.direction.value,
                    "strategy": self.setup.strategy, "candidate_t_ms": self.setup.candidate_t_ms,
                    "entry_tag": self.setup.entry_tag, "entry_price": self.setup.entry_price,
                    "entry_qty": self.setup.entry_qty, "stop_price": self.setup.stop_price,
                    "targets": [{"tag": t.tag, "level": t.level, "price": t.price, "qty": t.qty}
                               for t in self.setup.targets],
                    "entry_filled_qty": self.setup.entry_filled_qty, "remaining_qty": self.setup.remaining_qty,
                    "state": self.setup.state, "close_reason": self.setup.close_reason,
                    "tp_done": list(self.setup.tp_done)}}

    def restore(self, state: dict[str, Any]) -> None:
        if state.get("version") != SNAPSHOT_VERSION or state.get("cfg_digest") != self.cfg.digest():
            raise ValueError("engine snapshot does not match this frozen configuration")
        self.now_ms = state["now_ms"]
        self.nav_usdt, self.nav_t_ms = state["nav"]
        self.halted_reason = state["halted_reason"]
        self.seen_setup_ids = list(state["seen_setup_ids"])
        raw = state["setup"]
        if raw is None:
            self.setup = None
        else:
            self.setup = FunnelSetup(
                setup_id=raw["setup_id"], direction=Direction(raw["direction"]), strategy=raw["strategy"],
                candidate_t_ms=raw["candidate_t_ms"], entry_tag=raw["entry_tag"], entry_price=raw["entry_price"],
                entry_qty=raw["entry_qty"], stop_price=raw["stop_price"],
                targets=[TargetOrder(**t) for t in raw["targets"]], entry_filled_qty=raw["entry_filled_qty"],
                remaining_qty=raw["remaining_qty"], state=raw["state"], close_reason=raw["close_reason"],
                tp_done=list(raw["tp_done"]))
