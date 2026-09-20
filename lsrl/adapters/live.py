"""MICRO_LIVE execution adapter for LSRL_V1.

STATUS: implemented and unit-tested against a FAKE exchange only. UNARMED: it cannot
even be constructed unless every switch is on (see `assert_armed`), and the launch
ceremony / Settings validator still refuse `lsrl_v1` in production LIVE.

Safety properties enforced here (each has a test):
  * every order-MUTATING action passes the mandatory central risk authorization
    (`record_authorization` from the evaluated RiskManager verdict, then
    `require_authorized`) BEFORE any exchange call, in one guarded method;
  * entry orders go only through the audited EntryOrderSubmitter / maker_entry boundary
    (write-ahead intent, deterministic clientOid, reconcile-by-clientOid), never a raw
    transport call; the clock gate lives inside the order client;
  * the executor owns its OWN intent store, so the shared ExecutionService startup
    recovery never mis-classifies LSRL's limit-order protection as "unprotected";
  * it refuses to open a setup while ANY foreign position exists on the symbol (the same
    rule the dynamic-grid service uses: Bitget's position cannot preserve lineage);
  * every entry is a post-only limit; targets are reduce-only post-only limits; the
    stop is exchange-side. There is no market entry anywhere in this module;
  * any ambiguous exchange state fails closed: no NEW entries (`halt_reason`), protection
    and cancellation continue; a position whose stop cannot be placed is flattened.

NOT verified against a real exchange: Bitget plan-order / reduce-only semantics, the
fill-price attribution for stop fills, and coexistence with PositionManager. These are
listed as open items in the implementation report.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any, Callable

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.clock_safety import ClockUnsafe
from clients.schemas import TradePlan
from execution.entry_submitter import EntryOrderBlocked, EntryOrderSubmitter
from execution.maker_entry import submit_persistent_maker_entry
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.core.config import DEFAULT_CONFIG, STRATEGY_ID, LSRLConfig
from lsrl.core.types import (CancelOrder, Direction, FillEvent, Intent, OrderEvent, PlaceEntry, SetStop,
                             SetTargets, SymbolRules)
from risk.mandatory_authorization import (RiskAuthorizationError, RiskAuthorizationStore, record_authorization,
                                          require_authorized)

EPS = 1e-9
_TERMINAL = {"filled", "full_fill", "full-filled", "cancelled", "canceled", "rejected", "expired"}
_FILLED = {"filled", "full_fill", "full-filled"}


class LiveNotArmed(RuntimeError):
    pass


class LiveExecutorError(RuntimeError):
    pass


def assert_armed(settings: Any) -> None:
    """Every one of these must hold; there is deliberately no override and no default-on."""
    missing: list[str] = []
    if getattr(settings, "forward_paper_only", False):
        missing.append("FORWARD_PAPER_ONLY_is_true")
    if not getattr(settings, "execution_enabled", False):
        missing.append("EXECUTION_ENABLED_is_false")
    if str(getattr(settings, "execution_mode", "")).strip().upper() != "LIVE":
        missing.append("EXECUTION_MODE_is_not_LIVE")
    if not getattr(settings, "lsrl_microlive_armed", False):
        missing.append("LSRL_MICROLIVE_ARMED_is_false")
    if str(getattr(settings, "lsrl_symbol", "")).upper() != DEFAULT_CONFIG.symbol:
        missing.append("LSRL_SYMBOL_is_not_SOLUSDT")
    if missing:
        raise LiveNotArmed("MICRO_LIVE not armed: " + ", ".join(missing))


def _reason(exc: Exception) -> str:
    if isinstance(exc, ClockUnsafe):
        return f"CLOCK_GATE:{exc}"[:200]
    if isinstance(exc, RiskAuthorizationError):
        return f"RISK_AUTHORIZATION_REFUSED:{type(exc).__name__}:{exc}"[:200]
    if isinstance(exc, EntryOrderBlocked):
        return f"ENTRY_BLOCKED:{exc}"[:200]
    return f"{type(exc).__name__}:{exc}"[:200]


class LSRLLiveExecutor:
    def __init__(self, *, settings: Any, client: Any, submitter: EntryOrderSubmitter,
                 auth_store: RiskAuthorizationStore, gate: CentralRiskGate, state_store: Any,
                 rules: SymbolRules, cfg: LSRLConfig = DEFAULT_CONFIG,
                 sleep: Callable[[float], None] = time.sleep, log: logging.Logger | None = None) -> None:
        assert_armed(settings)
        self.settings, self.client, self.submitter = settings, client, submitter
        self.auth_store, self.gate, self.store = auth_store, gate, state_store
        self.rules, self.cfg, self._sleep = rules, cfg, sleep
        self.log = log or logging.getLogger("lsrl_live")
        data = state_store.load({}) or {}
        self.orders: dict[str, dict[str, Any]] = data.get("orders", {})
        self.tp: dict[str, dict[str, Any]] = data.get("tp", {})
        self.stop: dict[str, Any] | None = data.get("stop")
        self.meta: dict[str, dict[str, Any]] = data.get("meta", {})
        self.halt_reason: str | None = data.get("halt_reason")

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict[str, Any]:
        return {"orders": self.orders, "tp": self.tp, "stop": self.stop, "meta": self.meta,
                "halt_reason": self.halt_reason}

    def _persist(self) -> None:
        self.store.save(self.to_state())

    def _halt(self, reason: str) -> None:
        if self.halt_reason is None:
            self.halt_reason = reason
            self.log.critical("LSRL_LIVE_HALT | reason=%s | no new entries; protection continues", reason)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hold(direction: Direction) -> str:
        return "long" if direction is Direction.LONG else "short"

    def _verdict(self, setup_id: str):
        found = self.gate.verdicts.get(setup_id)
        if found is None:
            raise RiskAuthorizationError(f"no evaluated central RiskManager verdict for setup {setup_id}")
        return found

    def _rung_plan(self, it: PlaceEntry) -> tuple[TradePlan, SimpleNamespace]:
        verdict, candidate = self._verdict(it.setup_id)
        ts = it.expires_at_ms - self.cfg.entry_expiry_s * 1000
        identity = f"{STRATEGY_ID}_e{it.rung}"
        cid = deterministic_candidate_id(identity, self.rules.symbol, it.direction.value, ts)
        plan = TradePlan(
            candidate_id=cid, candidate_candle_open_timestamp_ms=ts, plan_id=deterministic_plan_id(cid),
            symbol=self.rules.symbol, strategy=STRATEGY_ID, direction=it.direction.value, verdict="EXECUTABLE",
            score=100.0, entry_prices=[it.price], stop_loss=float(candidate.detection.invalidation),
            take_profits=[], risk_reward_ratio=0.0, account_risk_pct=float(verdict.account_risk_pct),
            leverage=float(verdict.leverage), position_notional_usdt=it.price * it.qty,
            notes=[f"lsrl_setup_id={it.setup_id}", f"rung={it.rung}"], reasons=list(verdict.reasons))
        ident = SimpleNamespace(candidate_id=cid, plan_id=plan.plan_id, symbol=plan.symbol,
                                direction=plan.direction, strategy=identity)
        return plan, ident

    # ------------------------------------------------------------------ the ONE guarded gate
    def _authorize(self, plan: TradePlan) -> None:
        """Central risk authorization, recorded from the evaluated verdict and then REQUIRED.
        Every order-mutating path that can add exposure calls this before touching the exchange."""
        verdict, _candidate = self._verdict(self._setup_of(plan))
        record_authorization(verdict, plan, plan_id=plan.plan_id,
                             position_notional_usdt=plan.position_notional_usdt, store=self.auth_store)
        require_authorized(plan, store=self.auth_store)

    @staticmethod
    def _setup_of(plan: TradePlan) -> str:
        for note in plan.notes:
            if note.startswith("lsrl_setup_id="):
                return note.split("=", 1)[1]
        raise RiskAuthorizationError("plan carries no lsrl_setup_id")

    # ------------------------------------------------------------------ intents
    def apply(self, intents: list[Intent], t_ms: int) -> list[Any]:
        events: list[Any] = []
        assert_armed(self.settings)                       # re-checked on EVERY apply (kill switch)
        for it in intents:
            try:
                if isinstance(it, PlaceEntry):
                    events += self._place_entry(it, t_ms)
                elif isinstance(it, CancelOrder):
                    self._cancel(it)
                elif isinstance(it, SetStop):
                    events += self._set_stop(it, t_ms)
                elif isinstance(it, SetTargets):
                    self._set_targets(it, t_ms)
            except Exception as exc:                      # never raise into the trading loop
                events += self._on_failure(it, exc, t_ms)
        self._persist()
        return events

    def _on_failure(self, it: Intent, exc: Exception, t_ms: int) -> list[Any]:
        reason = _reason(exc)
        self.log.critical("LSRL_LIVE_ACTION_FAILED | %s | %s", type(it).__name__, reason)
        if isinstance(it, PlaceEntry):
            # A clean business rejection (e.g. post-only would cross) created no order and says
            # nothing about exchange state: the remaining rungs proceed. Anything else may leave an
            # order in an unknown state, so it halts NEW entries.
            if "status=REJECTED" not in reason:
                self._halt(f"ENTRY_FAILED:{reason}")
            return [OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", reason)]
        if isinstance(it, SetStop) and it.qty > 0:
            return self._protection_failed(it, reason, t_ms)
        if isinstance(it, (SetTargets, CancelOrder)):
            self._halt(f"MANAGEMENT_FAILED:{reason}")
        return []

    def _place_entry(self, it: PlaceEntry, t_ms: int) -> list[Any]:
        if it.tag in self.orders:                          # restart / duplicate delivery: already placed
            return []
        if self.halt_reason:
            return [OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", f"EXECUTOR_HALTED:{self.halt_reason}")]
        plan, ident = self._rung_plan(it)
        self._authorize(plan)                              # 1. mandatory central risk gate
        self._clock_gate()                                 # 2. clock gate, checked up front (also inside the client)
        self._preflight(it)                                # 3. foreign-position check + leverage
        result = submit_persistent_maker_entry(            # 4. audited boundary
            submitter=self.submitter, client=self.client, plan=ident, size=it.qty, price=it.price,
            notional_usdt=plan.position_notional_usdt, direction=it.direction.value)
        self.orders[it.tag] = {"tag": it.tag, "kind": "ENTRY", "setup_id": it.setup_id, "rung": it.rung,
                               "client_oid": result.client_oid, "order_id": result.order_id, "price": it.price,
                               "qty": it.qty, "direction": it.direction.value, "reported_qty": 0.0,
                               "reported_notional": 0.0, "reported_fee": 0.0, "terminal": False}
        return [OrderEvent(t_ms, it.setup_id, it.tag, "ACCEPTED")]

    def _clock_gate(self) -> None:
        """Refuse BEFORE any intent is written: a raw ClockUnsafe from inside the submitter would be
        classified 'unclassified, may exist' and churn the intent store for an order never sent."""
        gate = getattr(self.client, "_require_safe_clock_for_order", None)
        if gate is not None:
            gate()

    def _preflight(self, it: PlaceEntry) -> None:
        meta = self.meta.setdefault(it.setup_id, {})
        if meta.get("preflight_ok"):
            return
        payload = self.client.get_all_positions()          # raises -> fail closed (state unknown)
        for row in (payload or {}).get("data") or []:
            if str(row.get("symbol") or "").upper() != self.rules.symbol:
                continue
            if float(row.get("total") or row.get("size") or 0.0) > 0:
                raise LiveExecutorError("FOREIGN_POSITION_PRESENT")
        verdict, _c = self._verdict(it.setup_id)
        self.client.set_futures_leverage(self.rules.symbol, leverage=max(1, int(verdict.leverage)),
                                         hold_side=self._hold(it.direction), margin_mode="isolated")
        meta["preflight_ok"] = True

    def _cancel(self, it: CancelOrder) -> None:
        rec = self.orders.get(it.tag)
        if rec is None or rec.get("terminal"):
            return
        try:
            self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=rec["client_oid"])
        except Exception as exc:
            if "not exist" in str(exc).lower() or "no such" in str(exc).lower():
                return                                     # already filled/cancelled: poll reconciles the truth
            raise

    def _set_stop(self, it: SetStop, t_ms: int) -> list[Any]:
        hold = self._hold(it.direction)
        if it.qty <= 0:
            self.client.cancel_all_futures_tpsl_orders(symbol=self.rules.symbol, hold_side=hold)
            self.stop = None
            return []
        last: Exception | None = None
        for _attempt in range(3):                          # the position may lag the fill by a moment
            try:
                self.client.move_futures_stop_loss(symbol=self.rules.symbol, hold_side=hold,
                                                   trigger_price=it.price, direction=it.direction.value,
                                                   cleanup_existing=True, reason=it.reason)
                self.stop = {"setup_id": it.setup_id, "price": it.price, "qty": it.qty,
                             "direction": it.direction.value}
                return []
            except Exception as exc:                       # noqa: BLE001
                last = exc
                self._sleep(1.0)
        assert last is not None
        raise last

    def _protection_failed(self, it: SetStop, reason: str, t_ms: int) -> list[Any]:
        """A position without an exchange-side stop is flattened immediately."""
        self._halt(f"STOP_PLACEMENT_FAILED:{reason}")
        events: list[Any] = []
        try:
            result = self.client.close_futures_position_full(
                symbol=self.rules.symbol, direction=it.direction.value, size=it.qty, margin_mode="isolated",
                reason="lsrl_unprotected_position", cleanup_tpsl=False)
            price = float((result or {}).get("avg_price") or (result or {}).get("price") or it.price)
            events.append(FillEvent(t_ms, it.setup_id, f"lsrl-{it.setup_id}-emergency", "OTHER_CLOSE",
                                    price, it.qty, liquidity="taker"))
        except Exception as exc:                            # noqa: BLE001
            self._halt(f"EMERGENCY_CLOSE_FAILED:{_reason(exc)}")
        return events

    def _set_targets(self, it: SetTargets, t_ms: int) -> None:
        hold = self._hold(it.direction)
        desired = {t.level: t for t in it.targets}
        for level in list(self.tp):
            cur, want = self.tp[level], desired.get(level)
            if want is None or abs(want.price - cur["price"]) > EPS or abs(want.qty - cur["qty"]) > EPS:
                self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=cur["client_oid"])
                cur["cancel_requested"] = True             # keep POLLING it: a late fill must still be reported
                self.orders[cur["tag"]] = cur
                del self.tp[level]
        meta = self.meta.setdefault(it.setup_id, {})
        for level, tgt in desired.items():
            if level in self.tp:
                continue
            meta["tp_version"] = meta.get("tp_version", 0) + 1
            oid = f"lsrl{it.setup_id[:10]}{level.lower()}v{meta['tp_version']}"
            res = self.client.place_futures_limit_close_order(
                symbol=self.rules.symbol, hold_side=hold, size=tgt.qty, price=tgt.price,
                client_oid=oid, post_only=True)
            rec = {"tag": tgt.tag, "kind": level, "setup_id": it.setup_id, "rung": None, "client_oid": oid,
                   "order_id": str(((res or {}).get("data") or {}).get("orderId") or ""), "price": tgt.price,
                   "qty": tgt.qty, "direction": it.direction.value, "reported_qty": 0.0,
                   "reported_notional": 0.0, "reported_fee": 0.0, "terminal": False}
            self.tp[level] = rec
            self.orders[tgt.tag] = rec

    # ------------------------------------------------------------------ reconciliation
    def poll(self, t_ms: int) -> list[Any]:
        """Read the exchange and translate reality into engine events. Never places an order."""
        events: list[Any] = []
        for tag, rec in list(self.orders.items()):
            if rec.get("terminal"):
                continue
            found = self.client.find_order_by_client_oid(self.rules.symbol, rec["client_oid"])
            status = found.get("status")
            if status == "UNKNOWN":
                self._halt(f"ORDER_STATE_UNKNOWN:{rec['client_oid']}")
                continue
            if status == "ABSENT":
                rec["terminal"] = True
                self._halt(f"ORDER_ABSENT:{rec['client_oid']}")
                events.append(OrderEvent(t_ms, rec["setup_id"], tag, "CANCELLED", "ABSENT_ON_EXCHANGE"))
                continue
            metrics = self.client.extract_fill_metrics({"data": found.get("order") or {}})
            filled, avg = float(metrics["filled_qty"]), float(metrics["avg_price"])
            fee = abs(float(metrics["fee"]))
            if filled > rec["reported_qty"] + EPS:
                delta = filled - rec["reported_qty"]
                notional = filled * avg - rec["reported_notional"]
                price = notional / delta if delta > EPS and avg > 0 else rec["price"]
                events.append(FillEvent(
                    t_ms, rec["setup_id"], tag, rec["kind"], price, delta,
                    fee_usdt=max(0.0, fee - rec["reported_fee"]), liquidity="maker", rung=rec.get("rung")))
                rec["reported_qty"], rec["reported_notional"], rec["reported_fee"] = filled, filled * avg, fee
            state = str(metrics["state"]).lower()
            if state in _TERMINAL:
                rec["terminal"] = True
                if state not in _FILLED and rec["kind"] == "ENTRY":
                    events.append(OrderEvent(t_ms, rec["setup_id"], tag, "CANCELLED", state))
        events += self._detect_stop(t_ms)
        self._persist()
        return events

    def _detect_stop(self, t_ms: int) -> list[Any]:
        """A position smaller than our own fills explain was closed by the stop (or externally)."""
        if not self.stop:
            return []
        sid = self.stop["setup_id"]
        entered = sum(r["reported_qty"] for r in self.orders.values() if r["setup_id"] == sid and r["kind"] == "ENTRY")
        exited = sum(r["reported_qty"] for r in self.orders.values() if r["setup_id"] == sid and r["kind"] != "ENTRY")
        expected = entered - exited
        payload = self.client.get_all_positions()
        live = 0.0
        mark = self.stop["price"]
        for row in (payload or {}).get("data") or []:
            if str(row.get("symbol") or "").upper() == self.rules.symbol:
                live += float(row.get("total") or row.get("size") or 0.0)
                mark = float(row.get("markPrice") or mark)
        missing = expected - live
        if missing <= EPS:
            return []
        stop_price = float(self.stop["price"])
        self.log.critical("LSRL_LIVE_UNEXPLAINED_REDUCTION | expected=%s live=%s | attributing to the stop",
                          expected, live)
        # The closing fill price is not available from position state; the stop price is the best
        # (documented, ESTIMATED) attribution. Reported as kind=SL.
        if live <= EPS:
            self.stop = None
        return [FillEvent(t_ms, sid, f"lsrl-{sid}-stop", "SL", stop_price, missing, liquidity="taker")]

    def resume(self) -> None:
        """Explicit owner action only; never called by the runtime."""
        self.halt_reason = None
        self._persist()
