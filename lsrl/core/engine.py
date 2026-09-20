"""LSRLEngine: the ONE implementation of LSRL_V1 decision logic.

Every run mode (replay/backtest, shadow, paper, live) drives this exact class with
a stream of events and differs only in the adapter that turns its INTENTS into
orders/fills. The engine holds no notion of "mode" (structurally tested), reads
no clock, does no I/O, and emits only: PlaceEntry (post-only limit), CancelOrder,
SetStop and SetTargets. There is deliberately no market-order intent, so "no
chase" cannot be violated by a code path.
"""
from __future__ import annotations

import hashlib
from typing import Any

from lsrl.core.atr import WilderATR
from lsrl.core.config import SPEC, LSRLConfig
from lsrl.core.gates import RiskDecision, RiskGate, RiskProposal
from lsrl.core.ladder import build_geometry, size_ladder, split_tp_quantities
from lsrl.core.lifecycle import Ledger, cost_adjusted_break_even
from lsrl.core.pools import PoolTracker
from lsrl.core.risk_state import DailyRiskState
from lsrl.core.setup import (RUNG_CANCELLED, RUNG_FILLED, RUNG_OPEN, RUNG_PLANNED, RUNG_REJECTED,
                             RUNG_SKIPPED, STATE_AWAIT_RECLAIM, STATE_CLOSED, STATE_IN_POSITION,
                             STATE_LADDER_PENDING, RungState, Setup)
from lsrl.core.sweep import (ConsumptionEstimate, SweepDetector, SweepOutcome, WatchPhase,
                             boundary_of)
from lsrl.core.targets import rr_to_tp2, select_targets
from lsrl.core.types import (BarClosed, CancelOrder, DepthSnapshot, Direction, FillEvent, Intent,
                             NavEvent, OrderEvent, PlaceEntry, Record, SetStop, SetTargets, Side,
                             SymbolRules, TargetOrder, TimerTick, TradeTick)

EPS = 1e-9
SNAPSHOT_VERSION = 1


def _cid(pool_id: str, bar_open_ts: int) -> str:
    return hashlib.sha256(f"{pool_id}|{bar_open_ts}".encode()).hexdigest()[:12]


class LSRLEngine:
    def __init__(self, cfg: LSRLConfig, rules: SymbolRules, *, risk_gate: RiskGate,
                 tape_available: bool = False, maker_fee_rate: float | None = None,
                 taker_fee_rate: float | None = None, stop_slippage_bps: float | None = None) -> None:
        if rules.symbol != cfg.symbol:
            raise ValueError(f"engine is frozen to {cfg.symbol}, got {rules.symbol}")
        self.cfg = cfg
        self.rules = rules
        self.risk_gate = risk_gate
        self.maker_fee = cfg.assumed_maker_fee_rate if maker_fee_rate is None else maker_fee_rate
        self.taker_fee = cfg.assumed_taker_fee_rate if taker_fee_rate is None else taker_fee_rate
        self.stop_slip_bps = (cfg.assumed_stop_slippage_bps if stop_slippage_bps is None
                              else stop_slippage_bps)
        self.tracker = PoolTracker(cfg, rules.tick_size)
        self.detector = SweepDetector(cfg, rules.tick_size, tape_available=tape_available)
        self.atr1m = WilderATR(cfg.atr_period)
        self.atr15 = WilderATR(cfg.atr_period)
        self.daily = DailyRiskState()
        self.setup: Setup | None = None
        self.seen_setup_ids: list[str] = []
        self.nav_usdt: float | None = None
        self.nav_t_ms: int = 0
        self.now_ms: int = 0
        self.last_snapshot: DepthSnapshot | None = None
        self.last_bar1m_open_ts: int | None = None
        self.halted_reason: str | None = None
        self._intents: list[Intent] = []
        self._records: list[Record] = []

    # ================================================================ public API
    @property
    def active_setup_id(self) -> str | None:
        return self.setup.setup_id if self.setup else None

    def halt(self, reason: str) -> None:
        self.halted_reason = reason

    def resume(self) -> None:
        self.halted_reason = None

    def on_event(self, ev: Any) -> tuple[list[Intent], list[Record]]:
        self._intents, self._records = [], []
        if isinstance(ev, (DepthSnapshot, TradeTick, BarClosed)) and ev.t_ms < self.now_ms:
            self._record("anomaly", ev.t_ms, {"reason": "OUT_OF_ORDER_MARKET_EVENT",
                                              "event_type": ev.TYPE, "engine_now_ms": self.now_ms})
            return self._intents, self._records
        self.now_ms = max(self.now_ms, ev.t_ms)
        if isinstance(ev, DepthSnapshot):
            self._on_depth(ev)
        elif isinstance(ev, TradeTick):
            self.detector.on_trade(ev)
        elif isinstance(ev, BarClosed):
            self._on_bar(ev)
        elif isinstance(ev, NavEvent):
            self.nav_usdt, self.nav_t_ms = ev.nav_usdt, ev.t_ms
        elif isinstance(ev, FillEvent):
            self._on_fill(ev)
        elif isinstance(ev, OrderEvent):
            self._on_order(ev)
        elif isinstance(ev, TimerTick):
            pass
        self._housekeeping()
        return self._intents, self._records

    # ================================================================ market data
    def _record(self, kind: str, t_ms: int, data: dict[str, Any]) -> None:
        self._records.append(Record(kind, t_ms, data))

    def _on_depth(self, snap: DepthSnapshot) -> None:
        if not snap.crossed and snap.bids and snap.asks:
            self.last_snapshot = snap
        self.tracker.update(snap, self.atr15.value)
        for note in self.detector.on_snapshot(snap, self.tracker, self.atr15.value):
            self._record("setup_rejected", snap.t_ms, {"stage": "POOL", **note})

    def _on_bar(self, bar: BarClosed) -> None:
        if bar.tf_s == self.cfg.setup_tf_s:
            self.atr15.update(bar.high, bar.low, bar.close)
            return
        if bar.tf_s != self.cfg.trigger_tf_s:
            return
        if self.last_bar1m_open_ts is not None and bar.open_ts_ms != self.last_bar1m_open_ts + 60_000:
            self._on_bar_gap(bar)
        self.last_bar1m_open_ts = bar.open_ts_ms
        self.atr1m.update(bar.high, bar.low, bar.close)
        for outcome in self.detector.on_bar_1m(bar, self.last_snapshot):
            if outcome.kind == "CONFIRMED":
                self._on_sweep_confirmed(outcome)
            else:
                self._record("setup_rejected", bar.t_ms, self._sweep_rejection(outcome))
        if self.setup is not None and self.setup.state == STATE_AWAIT_RECLAIM:
            self._advance_reclaim(bar)

    def _on_bar_gap(self, bar: BarClosed) -> None:
        """A missing 1m bar breaks the causal chain: drop every in-flight attempt."""
        self._record("anomaly", bar.t_ms, {"reason": "BAR_FEED_GAP", "bar_open_ts": bar.open_ts_ms,
                                           "expected_open_ts": (self.last_bar1m_open_ts or 0) + 60_000})
        for w in self.detector.watches.values():
            if w.phase is WatchPhase.INTERACTING:
                self._record("setup_rejected", bar.t_ms, {
                    "stage": "SWEEP", "reason": "BAR_FEED_GAP", "pool_id": w.pool_id,
                    "pool_frozen": w.frozen.to_dict() if w.frozen else None})
                w.phase = WatchPhase.DONE
        if self.setup is not None and self.setup.state == STATE_AWAIT_RECLAIM:
            self._reject_setup("BAR_FEED_GAP", bar.t_ms)

    # ================================================================ sweep -> setup
    def _sweep_rejection(self, o: SweepOutcome) -> dict[str, Any]:
        w = o.watch
        return {
            "stage": "SWEEP", "reason": o.reason, "candidate_id": _cid(w.pool_id, o.bar.open_ts_ms),
            "pool_id": w.pool_id, "direction": w.direction.value, "side": w.side.value,
            "pool_frozen": w.frozen.to_dict() if w.frozen else None,
            "pool_first_seen_ms": w.first_seen_ms, "frozen_failures": list(w.frozen_failures),
            "interaction_start_ms": w.interaction_start_ms, "interaction_source": w.interaction_source,
            "interaction_bars": w.interaction_bars, "sweep_extreme": w.extreme,
            "consumption": self._est_dict(o.consumption), "depth_series": [list(x) for x in w.depth_series],
            "bar": {"open_ts_ms": o.bar.open_ts_ms, "high": o.bar.high, "low": o.bar.low,
                    "close": o.bar.close},
        }

    @staticmethod
    def _est_dict(est: ConsumptionEstimate | None) -> dict[str, Any] | None:
        if est is None:
            return None
        return {"method": est.method, "frozen_depth": est.frozen_depth, "consumed": est.consumed,
                "removed_total": est.removed_total, "cancelled_est": est.cancelled_est,
                "fraction": est.fraction}

    def _on_sweep_confirmed(self, o: SweepOutcome) -> None:
        w, bar = o.watch, o.bar
        assert w.frozen is not None and w.extreme is not None
        setup_id = hashlib.sha256(
            f"{SPEC}|{self.cfg.symbol}|{w.direction.value}|{w.pool_id}|{bar.open_ts_ms}".encode()
        ).hexdigest()[:16]
        reject: str | None = None
        if setup_id in self.seen_setup_ids:
            reject = "DUPLICATE_SETUP"
        elif self.setup is not None:
            reject = "EXISTING_LSRL_SETUP_ACTIVE"
        elif self.daily.blocked(bar.t_ms):
            reject = "DAILY_STOP_ACTIVE"
        elif self.halted_reason:
            reject = f"ENGINE_HALTED:{self.halted_reason}"
        if reject:
            row = self._sweep_rejection(o)
            row.update(reason=reject, setup_id=setup_id)
            self._record("setup_rejected", bar.t_ms, row)
            return
        est = o.consumption
        assert est is not None
        self.seen_setup_ids.append(setup_id)
        del self.seen_setup_ids[:-1000]
        self.setup = Setup(
            setup_id=setup_id, direction=w.direction, symbol=self.cfg.symbol,
            state=STATE_AWAIT_RECLAIM, candidate_t_ms=bar.t_ms, pool=w.frozen.to_dict(),
            pool_id=w.pool_id, pool_first_seen_ms=w.first_seen_ms,
            pool_frozen_t_ms=w.frozen.t_ms, pool_frozen_mid=w.frozen_mid,
            pool_age_s_at_freeze=(w.frozen.t_ms - w.first_seen_ms) / 1000.0,
            pool_in_range_since_ms=w.frozen_in_range_since_ms,
            boundary=boundary_of(w.frozen), interaction_start_ms=w.interaction_start_ms or bar.open_ts_ms,
            interaction_source=w.interaction_source, sweep_bar_open_ts=bar.open_ts_ms,
            sweep_t_ms=bar.t_ms, extreme=w.extreme, consumption=self._est_dict(est) or {},
            depth_series=[list(x) for x in w.depth_series], tape_trades=w.tape_trades,
            tape_consumed=w.tape_consumed,
        )
        self.setup.event(bar.t_ms, "SWEEP_CONFIRMED", fraction=est.fraction, method=est.method)
        self._record("lifecycle", bar.t_ms, {"setup_id": setup_id, "event": "SWEEP_CONFIRMED",
                                             "sweep_extreme": w.extreme, "boundary": self.setup.boundary,
                                             "consumption": self._est_dict(est)})

    def _advance_reclaim(self, bar: BarClosed) -> None:
        s = self.setup
        assert s is not None
        d = s.direction
        s.extreme = min(s.extreme, bar.low) if d is Direction.LONG else max(s.extreme, bar.high)
        reclaimed = bar.close > s.boundary if d is Direction.LONG else bar.close < s.boundary
        if reclaimed:
            s.reclaim_bar_open_ts, s.reclaim_t_ms = bar.open_ts_ms, bar.t_ms
            self._on_reclaim(bar)
        elif s.bars_after_sweep >= self.cfg.reclaim_max_bars_after_sweep:
            self._reject_setup("NO_RECLAIM_WITHIN_5_BARS", bar.t_ms)
        else:
            s.bars_after_sweep += 1

    # ================================================================ decision pipeline
    def _reject_setup(self, reason: str, t_ms: int, *, stage: str = "SETUP") -> None:
        s = self.setup
        assert s is not None
        s.close_reason = reason
        s.state = STATE_CLOSED
        self._record("setup_rejected", t_ms, s.to_record(stage=stage, reason=reason, t_ms=t_ms))
        self.setup = None

    def _absorption(self, s: Setup) -> dict[str, Any]:
        if not self.detector.tape_available or s.tape_trades <= 0:
            return {"available": False, "reason": "NO_CAUSAL_TAPE", "confirmed": None}
        frozen_depth = float(s.pool["depth"])
        far_edge = float(s.pool["lower"] if s.direction is Direction.LONG else s.pool["upper"])
        beyond = s.direction.sgn * (far_edge - s.extreme)
        atr1m = self.atr1m.value or 0.0
        flow_ok = s.tape_consumed >= self.cfg.sweep_min_consumed_fraction * frozen_depth
        no_progress = beyond <= self.cfg.sl_atr1m_mult * atr1m
        return {"available": True, "aggressive_flow_in_pool": s.tape_consumed,
                "frozen_depth": frozen_depth, "progress_beyond_pool": beyond,
                "flow_consistent_with_sweep": flow_ok, "no_material_progress_beyond_pool": no_progress,
                "confirmed": bool(flow_ok and no_progress), "gating": False}

    def _on_reclaim(self, bar: BarClosed) -> None:
        s = self.setup
        assert s is not None
        t = bar.t_ms
        snap = self.last_snapshot
        s.atr15, s.atr1m = self.atr15.value, self.atr1m.value
        s.absorption = self._absorption(s)
        if snap is None or (t - snap.t_ms) / 1000.0 > self.cfg.max_snapshot_age_s:
            return self._reject_setup("LIQUIDITY_DATA_STALE", t)
        s.spread_bps = snap.spread_bps
        if s.atr1m is None or s.atr15 is None:
            return self._reject_setup("ATR_UNAVAILABLE", t)
        if snap.spread_bps > self.cfg.max_spread_bps:
            return self._reject_setup("SPREAD_GATE", t)
        geometry, why = build_geometry(s.direction, s.boundary, s.extreme, s.atr1m, self.rules, self.cfg)
        if geometry is None:
            return self._reject_setup(why, t)
        s.geometry = {"boundary": geometry.boundary, "extreme": geometry.extreme,
                      "rungs": list(geometry.rung_prices), "stop": geometry.stop}
        s.stop_price = geometry.stop
        if self.nav_usdt is None or (t - self.nav_t_ms) / 1000.0 > self.cfg.max_nav_age_s:
            return self._reject_setup("NAV_UNAVAILABLE", t)
        sizing, why = size_ladder(geometry, self.nav_usdt, self.cfg.risk_per_full_trade_frac, self.rules,
                                  self.cfg, maker_fee_rate=self.maker_fee, taker_fee_rate=self.taker_fee,
                                  stop_slippage_bps=self.stop_slip_bps)
        if sizing is None:
            return self._reject_setup(why, t)
        targets, why = select_targets(s.direction, self.tracker, snap, t, s.atr15,
                                      geometry.rung_prices[0], self.rules, self.cfg)
        if targets is None:
            self._store_sizing(s, sizing)
            return self._reject_setup(why, t)
        s.targets = [{"level": x.level, "price": x.price, "pool_id": x.pool_id,
                      "pool_strength": x.pool_strength, "pool_depth": x.pool_depth,
                      "pool_age_s": x.pool_age_s} for x in targets]
        self._store_sizing(s, sizing)
        slices = split_tp_quantities(sizing.total_qty, self.rules, self.cfg, merge_below_min=False)
        if any(q * tp.price < self.rules.min_notional_usdt for (_l, q), tp in zip(slices, targets)):
            return self._reject_setup("BELOW_MIN_NOTIONAL_TP_SLICE", t)
        rr = rr_to_tp2(s.direction, sizing.expected_avg_entry, geometry.stop, targets[1].price)
        s.expected_rr_tp2 = rr
        if rr < self.cfg.min_rr_to_tp2 - EPS:
            return self._reject_setup("RR_TO_TP2_BELOW_MIN", t)
        try:
            decision = self.risk_gate.check(RiskProposal(
                setup_id=s.setup_id, symbol=s.symbol, direction=s.direction, t_ms=t,
                nav_usdt=self.nav_usdt, entry_prices=geometry.rung_prices, stop=geometry.stop,
                total_qty=sizing.total_qty,
                planned_notional_usdt=sum(sizing.rung_qty * p for p in geometry.rung_prices),
                planned_loss_usdt=sizing.planned_loss_usdt,
                strategy_risk_fraction=self.cfg.risk_per_full_trade_frac,
                spread_bps=snap.spread_bps))
        except Exception as exc:                       # fail closed on ANY gate error
            decision = RiskDecision(False, (f"RISK_GATE_ERROR:{type(exc).__name__}",), None, "ERROR")
        s.risk_gate = {"allowed": decision.allowed, "reasons": list(decision.reasons),
                       "applied_risk_fraction": decision.applied_risk_fraction, "source": decision.source}
        if not decision.allowed:
            return self._reject_setup("RISK_MANAGER_REJECTED", t)
        applied = decision.applied_risk_fraction
        if applied is not None and applied < self.cfg.risk_per_full_trade_frac - EPS:
            resized, why = size_ladder(geometry, self.nav_usdt, applied, self.rules, self.cfg,
                                       maker_fee_rate=self.maker_fee, taker_fee_rate=self.taker_fee,
                                       stop_slippage_bps=self.stop_slip_bps)
            if resized is None:
                return self._reject_setup(f"RISK_REDUCED_{why}", t)
            sizing = resized
            self._store_sizing(s, sizing)
        self._place_ladder(s, geometry, sizing, snap, t)

    @staticmethod
    def _store_sizing(s: Setup, sizing: Any) -> None:
        s.sizing = {"risk_fraction": sizing.risk_fraction, "risk_budget_usdt": sizing.risk_budget_usdt,
                    "rung_qty": sizing.rung_qty, "total_qty": sizing.total_qty,
                    "planned_loss_usdt": sizing.planned_loss_usdt,
                    "expected_avg_entry": sizing.expected_avg_entry,
                    "est_entry_fees_usdt": sizing.est_entry_fees_usdt,
                    "est_stop_exit_fee_usdt": sizing.est_stop_exit_fee_usdt,
                    "est_stop_slippage_usdt": sizing.est_stop_slippage_usdt}
        s.planned_loss_usdt = sizing.planned_loss_usdt

    def _place_ladder(self, s: Setup, geometry: Any, sizing: Any, snap: DepthSnapshot, t: int) -> None:
        d = s.direction
        expires = t + self.cfg.entry_expiry_s * 1000
        s.entry_expires_ms = expires
        s.ledger = Ledger(direction=d)
        placed = 0
        for i, price in enumerate(geometry.rung_prices, start=1):
            tag = f"lsrl-{s.setup_id}-e{i}"
            crosses = price >= snap.best_ask if d is Direction.LONG else price <= snap.best_bid
            rung = RungState(rung=i, tag=tag, price=price, qty=sizing.rung_qty)
            if crosses:
                rung.status, rung.reject_reason = RUNG_SKIPPED, "WOULD_CROSS_NO_CHASE"
            else:
                placed += 1
                self._intents.append(PlaceEntry(setup_id=s.setup_id, tag=tag, rung=i, direction=d,
                                                price=price, qty=sizing.rung_qty, expires_at_ms=expires))
            s.rungs[i] = rung
        if placed == 0:
            return self._reject_setup("EXECUTION_GATE_ALL_RUNGS_WOULD_CROSS", t)
        s.state = STATE_LADDER_PENDING
        s.event(t, "LADDER_PLACED", rungs=[r.to_state() for r in s.rungs.values()], expires_ms=expires)
        self._record("setup_accepted", t, s.to_record(stage="ACCEPTED", reason="LADDER_PLACED", t_ms=t))

    # ================================================================ order / fills
    def _on_order(self, ev: OrderEvent) -> None:
        s = self.setup
        if s is None or ev.setup_id != s.setup_id:
            self._record("anomaly", ev.t_ms, {"reason": "ORDER_EVENT_FOR_UNKNOWN_SETUP",
                                              "setup_id": ev.setup_id, "tag": ev.tag, "status": ev.status})
            return
        rung = next((r for r in s.rungs.values() if r.tag == ev.tag), None)
        if rung is None:
            s.event(ev.t_ms, "ORDER_EVENT", tag=ev.tag, status=ev.status, reason=ev.reason)
            return
        if ev.status == "ACCEPTED" and rung.status == RUNG_PLANNED:
            rung.status = RUNG_OPEN
        elif ev.status in ("REJECTED", "CANCELLED", "EXPIRED") and rung.status in (RUNG_PLANNED, RUNG_OPEN):
            rung.status = RUNG_REJECTED if ev.status == "REJECTED" else RUNG_CANCELLED
            rung.reject_reason = ev.reason
        s.event(ev.t_ms, "ORDER_EVENT", tag=ev.tag, status=ev.status, reason=ev.reason)
        led = s.ledger
        if (s.state == STATE_LADDER_PENDING and led is not None and led.entry_qty <= EPS
                and not s.live_rungs()):
            reasons = sorted({r.reject_reason for r in s.rungs.values() if r.reject_reason})
            self._reject_setup("ENTRY_ORDERS_REJECTED:" + "|".join(reasons), ev.t_ms, stage="EXECUTION")

    def _on_fill(self, f: FillEvent) -> None:
        s = self.setup
        if s is None or f.setup_id != s.setup_id or s.ledger is None:
            self._record("anomaly", f.t_ms, {"reason": "FILL_FOR_UNKNOWN_SETUP", "setup_id": f.setup_id,
                                             "tag": f.tag, "kind": f.kind, "price": f.price, "qty": f.qty})
            return
        led, d = s.ledger, s.direction
        if f.kind == "ENTRY":
            rung = s.rungs.get(f.rung or -1)
            if rung is not None:
                rung.filled_qty += f.qty
                if rung.filled_qty >= rung.qty - EPS:
                    rung.status = RUNG_FILLED
                s.entry_slippage_usdt += d.sgn * (f.price - rung.price) * f.qty
            led.on_entry(f.price, f.qty, f.fee_usdt)
            if s.state == STATE_LADDER_PENDING:
                s.state = STATE_IN_POSITION
            s.event(f.t_ms, "ENTRY_FILL", rung=f.rung, price=f.price, qty=f.qty, fee=f.fee_usdt)
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": "ENTRY_FILL",
                                               "rung": f.rung, "price": f.price, "qty": f.qty})
            self._sync_protection(s, "ENTRY_FILL")
            return
        if f.kind in ("TP1", "TP2", "TP3"):
            led.on_exit(f.price, f.qty, f.fee_usdt)
            order = s.tp_orders.get(f.kind)
            if order is not None:
                order["filled"] = order.get("filled", 0.0) + f.qty
            s.event(f.t_ms, f.kind + "_FILL", price=f.price, qty=f.qty, fee=f.fee_usdt)
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": f.kind + "_FILL",
                                               "price": f.price, "qty": f.qty})
            complete = order is not None and order["filled"] >= order["qty"] - EPS
            if complete and f.kind not in s.tp_done:
                s.tp_done.append(f.kind)
                self._after_tp(s, f.kind, f.t_ms)
            if led.remaining_qty <= EPS:
                self._close_setup("TP3_FLAT" if f.kind == "TP3" else "FLAT_AFTER_" + f.kind, f.t_ms)
            return
        if f.kind == "SL":
            led.on_exit(f.price, f.qty, f.fee_usdt)
            s.stop_slippage_usdt += d.sgn * ((s.stop_price or f.price) - f.price) * f.qty
            s.event(f.t_ms, "SL_FILL", price=f.price, qty=f.qty, fee=f.fee_usdt)
            self._record("lifecycle", f.t_ms, {"setup_id": s.setup_id, "event": "SL_FILL",
                                               "price": f.price, "qty": f.qty})
            if led.remaining_qty <= EPS:
                self._close_setup("STOP_LOSS", f.t_ms)
            return
        if f.kind == "OTHER_CLOSE":
            led.on_exit(f.price, f.qty, f.fee_usdt)
            s.event(f.t_ms, "OTHER_CLOSE", price=f.price, qty=f.qty)
            if led.remaining_qty <= EPS:
                self._close_setup("CLOSED_EXTERNALLY", f.t_ms)

    def _desired_targets(self, s: Setup) -> tuple[TargetOrder, ...]:
        assert s.ledger is not None and s.targets is not None
        basis = s.ledger.entry_qty
        slices = split_tp_quantities(basis, self.rules, self.cfg, merge_below_min=True)
        by_level = {t["level"]: t for t in s.targets}
        out: list[TargetOrder] = []
        for level, qty in slices:
            if qty <= EPS or level in s.tp_done:
                continue
            tag = f"lsrl-{s.setup_id}-{level.lower()}"
            prior = s.tp_orders.get(level, {})
            s.tp_orders[level] = {"tag": tag, "price": by_level[level]["price"], "qty": qty,
                                  "filled": prior.get("filled", 0.0)}
            out.append(TargetOrder(tag=tag, level=level, price=by_level[level]["price"], qty=qty))
        return tuple(out)

    def _sync_protection(self, s: Setup, reason: str) -> None:
        """Exchange-side stop for the whole position, then the resting TP set."""
        assert s.ledger is not None and s.stop_price is not None
        self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction, price=s.stop_price,
                                     qty=s.ledger.remaining_qty, reason=reason))
        if s.targets:
            self._intents.append(SetTargets(setup_id=s.setup_id, direction=s.direction,
                                            targets=self._desired_targets(s), reason=reason))

    def _cancel_entries(self, s: Setup, reason: str, t_ms: int) -> None:
        for r in s.live_rungs():
            self._intents.append(CancelOrder(setup_id=s.setup_id, tag=r.tag, reason=reason))
            r.status = RUNG_CANCELLED
            r.reject_reason = reason
        s.entries_cancelled = True
        s.event(t_ms, "ENTRIES_CANCELLED", reason=reason)

    def _after_tp(self, s: Setup, level: str, t_ms: int) -> None:
        assert s.ledger is not None
        led = s.ledger
        if level == "TP1":
            self._cancel_entries(s, "ENTRIES_CANCELLED_ON_TP1", t_ms)
            be = cost_adjusted_break_even(led, self.rules.tick_size, taker_fee_rate=self.taker_fee,
                                          stop_slippage_bps=self.stop_slip_bps)
            snap = self.last_snapshot
            valid = be is not None and snap is not None and (
                be < snap.best_bid if s.direction is Direction.LONG else be > snap.best_ask)
            if valid:
                s.stop_price = be
                s.event(t_ms, "STOP_MOVED_TO_COST_ADJUSTED_BE", price=be)
                self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction, price=be,
                                             qty=led.remaining_qty, reason="TP1_COST_ADJUSTED_BE"))
            else:
                s.event(t_ms, "BE_STOP_INVALID_KEPT_PRIOR", candidate=be, kept=s.stop_price)
        elif level == "TP2":
            tp1 = s.tp_orders["TP1"]["price"]
            s.stop_price = tp1
            s.event(t_ms, "STOP_MOVED_TO_TP1", price=tp1)
            self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction, price=tp1,
                                         qty=led.remaining_qty, reason="TP2_STOP_TO_TP1"))

    # ================================================================ close / timers
    def _close_setup(self, reason: str, t_ms: int) -> None:
        s = self.setup
        assert s is not None and s.ledger is not None
        for r in s.live_rungs():
            self._intents.append(CancelOrder(setup_id=s.setup_id, tag=r.tag, reason=reason))
            r.status = RUNG_CANCELLED
        if s.tp_orders:
            self._intents.append(SetTargets(setup_id=s.setup_id, direction=s.direction, targets=(),
                                            reason=reason))
        if s.stop_price is not None and s.ledger.entry_qty > 0:
            self._intents.append(SetStop(setup_id=s.setup_id, direction=s.direction,
                                         price=s.stop_price, qty=0.0, reason=reason))
        s.state = STATE_CLOSED
        s.close_reason = reason
        s.realized_net_usdt = s.ledger.realized_net()
        s.realized_r = (s.realized_net_usdt / s.planned_loss_usdt) if s.planned_loss_usdt > 0 else 0.0
        if s.ledger.entry_qty > 0:
            self.daily.record_close(t_ms, s.realized_r, self.cfg)
        s.event(t_ms, "SETUP_CLOSED", reason=reason, realized_net_usdt=s.realized_net_usdt,
                realized_r=s.realized_r)
        self._record("setup_closed", t_ms, s.to_record(stage="CLOSED", reason=reason, t_ms=t_ms))
        self.setup = None

    def _housekeeping(self) -> None:
        s = self.setup
        if s is None or s.state not in (STATE_LADDER_PENDING, STATE_IN_POSITION) or s.ledger is None:
            return
        now = self.now_ms
        if not s.entries_cancelled and s.live_rungs():
            expired = s.entry_expires_ms is not None and now >= s.entry_expires_ms
            snap = self.last_snapshot
            stale = snap is None or (now - snap.t_ms) / 1000.0 > self.cfg.max_snapshot_age_s
            if expired or stale:
                self._cancel_entries(s, "ENTRY_EXPIRED" if expired else "LIQUIDITY_DATA_STALE", now)
        if s.entries_cancelled and s.ledger.entry_qty <= EPS and not s.live_rungs():
            reason = "ENTRY_EXPIRED_NO_FILL"
            if any(e["kind"] == "ENTRIES_CANCELLED" and e.get("reason") == "LIQUIDITY_DATA_STALE"
                   for e in s.events):
                reason = "LIQUIDITY_DATA_STALE"
            self._close_setup(reason, now)

    # ================================================================ persistence
    def snapshot(self) -> dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION, "cfg_digest": self.cfg.digest(), "now_ms": self.now_ms,
            "nav": [self.nav_usdt, self.nav_t_ms], "atr1m": self.atr1m.state(),
            "atr15": self.atr15.state(), "last_bar1m_open_ts": self.last_bar1m_open_ts,
            "daily": self.daily.to_dict(), "seen_setup_ids": list(self.seen_setup_ids),
            "halted_reason": self.halted_reason,
            "setup": self.setup.to_state() if self.setup else None,
        }

    def restore(self, state: dict[str, Any]) -> None:
        if state.get("version") != SNAPSHOT_VERSION or state.get("cfg_digest") != self.cfg.digest():
            raise ValueError("engine snapshot does not match this frozen configuration")
        self.now_ms = state["now_ms"]
        self.nav_usdt, self.nav_t_ms = state["nav"]
        self.atr1m = WilderATR.from_state(self.cfg.atr_period, state["atr1m"])
        self.atr15 = WilderATR.from_state(self.cfg.atr_period, state["atr15"])
        self.last_bar1m_open_ts = state["last_bar1m_open_ts"]
        self.daily = DailyRiskState.from_dict(state["daily"])
        self.seen_setup_ids = list(state["seen_setup_ids"])
        self.halted_reason = state["halted_reason"]
        self.setup = Setup.from_state(state["setup"]) if state["setup"] else None
