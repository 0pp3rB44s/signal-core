"""MICRO_LIVE execution adapter for LSRL_V1.

STATUS: implemented and tested against a FAKE exchange only (whose behaviours are limited to what the
official Bitget documentation and this repository's own incident notes establish). UNARMED: it cannot even
be constructed unless every switch is on (see `assert_armed`), and the launch ceremony / Settings validator
still refuse `lsrl_v1` in production LIVE.

Safety properties (each has a test):
  * every order-MUTATING action that can add exposure passes the mandatory central risk authorization
    (`record_authorization` from the evaluated RiskManager verdict, then `require_authorized`) BEFORE any
    exchange call; entries go only through the audited `EntryOrderSubmitter` / maker-entry boundary;
  * STARTUP FACTS: contract metadata (tick = priceEndStep x 10**-pricePlace ...), symbol status, HEDGE
    position mode and ISOLATED margin are verified from the exchange before the first entry; anything
    unproven halts (`lsrl.contract`);
  * a stop is PROTECTION only once the exchange has been READ BACK showing it: the client's own verdict
    (`verified`) AND an independent plan-order read (right side, right trigger, covering the position, no
    stray stop) are both required; the active stop is audited on every poll; a stop that cannot be
    (re)established flattens the position (cancelling only OUR resting take-profits first) and halts;
  * a cancel is a REQUEST: the order stays tracked until the exchange shows it terminal; a fill that lands in
    the cancel race is delivered as an incremental FillEvent and the rung is reported CANCELLED only after
    that final fill and a position reconciliation (no orphan exposure);
  * fills are cumulative per clientOid and monotonic: a delta is reported exactly once, never twice; stop
    fills come from the exchange ORDER HISTORY (real price/fee), never from position shrinkage;
  * every expected take-profit is reconciled against the exchange: FILLED / CANCELLED (requested or not) /
    UNKNOWN. A take-profit the exchange dropped is re-placed idempotently for the unfilled remainder;
  * EXACTLY ONE OWNER of LSRL exposure: (symbol, hold side) is claimed in the strategy-ownership registry
    before the first order and released only when orders and exposure are gone, so the legacy position
    monitor can never adopt it;
  * before every entry cycle AND on every poll, exposure the ledger does not explain (a foreign SOLUSDT
    position) HALTS the executor with an explicit incident; strategy ownership is never merged silently;
  * every finalized trade is written through the CANONICAL central close pipeline (provisional row ->
    exchange-truth economics -> economic CLOSE) and NEW entries stay blocked until that record exists;
  * any ambiguous exchange state fails closed: no NEW entries (`halt_reason`, persisted, explicit
    `resume()` only); protection, cancellation and reconciliation continue.
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
from execution.strategy_ownership import (OwnershipConflict, OwnershipUnreadable,
                                          StrategyOwnershipRegistry)
from lsrl.adapters.central_ledger import RECORDED, CentralCloseRecorder
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.contract import ExchangeFactsError, verify_exchange_facts
from lsrl.core.config import DEFAULT_CONFIG, STRATEGY_ID, LSRLConfig
from lsrl.core.types import (CancelOrder, Direction, FillEvent, Intent, OrderEvent, PlaceEntry, SetStop,
                             SetTargets, SymbolRules)
from risk.mandatory_authorization import (RiskAuthorizationError, RiskAuthorizationStore, record_authorization,
                                          require_authorized)

EPS = 1e-9
STATE_VERSION = 2
_TERMINAL = {"filled", "full_fill", "full-filled", "cancelled", "canceled", "rejected", "expired"}
_FILLED = {"filled", "full_fill", "full-filled"}
_STOP_SOURCES = ("loss_market", "loss_limit", "pos_loss_market", "pos_loss_limit")
STOP_RETRIES = 3
TP_PLACE_ATTEMPTS = 5
MISMATCH_ROUNDS = 3
CENTRAL_RETRY_S = 5.0
CENTRAL_MAX_ATTEMPTS = 120
EMERGENCY_MAX_ATTEMPTS = 10
MAX_INCIDENTS = 200


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


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        for key in ("entrustedList", "list", "orderList", "planList"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return []
    return [r for r in (data or []) if isinstance(r, dict)]


class LSRLLiveExecutor:
    def __init__(self, *, settings: Any, client: Any, submitter: EntryOrderSubmitter,
                 auth_store: RiskAuthorizationStore, gate: CentralRiskGate, state_store: Any,
                 rules: SymbolRules, cfg: LSRLConfig = DEFAULT_CONFIG,
                 sleep: Callable[[float], None] = time.sleep, log: logging.Logger | None = None,
                 ownership: StrategyOwnershipRegistry | None = None,
                 central: CentralCloseRecorder | None = None,
                 facts_verifier: Callable[..., Any] = verify_exchange_facts) -> None:
        assert_armed(settings)
        self.settings, self.client, self.submitter = settings, client, submitter
        self.auth_store, self.gate, self.store = auth_store, gate, state_store
        self.rules, self.cfg, self._sleep = rules, cfg, sleep
        self.log = log or logging.getLogger("lsrl_live")
        self.ownership = ownership or StrategyOwnershipRegistry()
        self.product_type = str(getattr(settings, "bitget_product_type", "USDT-FUTURES")).upper()
        self.central = central or CentralCloseRecorder(client=client, product_type=self.product_type)
        self._verify_facts = facts_verifier
        data = state_store.load({}) or {}
        if data and data.get("version") != STATE_VERSION:
            raise LiveExecutorError(f"executor state version {data.get('version')} != {STATE_VERSION}")
        self.orders: dict[str, dict[str, Any]] = data.get("orders", {})        # keyed by clientOid
        self.setups: dict[str, dict[str, Any]] = data.get("setups", {})
        self.stop: dict[str, Any] | None = data.get("stop")
        self.stop_fills: dict[str, dict[str, float]] = data.get("stop_fills", {})
        self.pending_central: dict[str, dict[str, Any]] = data.get("pending_central", {})
        self.incidents: list[dict[str, Any]] = data.get("incidents", [])
        self.mismatch_polls: int = int(data.get("mismatch_polls", 0))
        self.emergency: dict[str, Any] | None = data.get("emergency")
        self.ledger_adj: dict[str, float] = data.get("ledger_adj", {})         # closes the exchange order book cannot show
        self.facts: dict[str, Any] | None = data.get("facts")
        self.halt_reason: str | None = data.get("halt_reason")
        self._facts_ok = False

    # ------------------------------------------------------------------ persistence / incidents
    def to_state(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, "orders": self.orders, "setups": self.setups, "stop": self.stop,
                "stop_fills": self.stop_fills, "pending_central": self.pending_central,
                "incidents": self.incidents[-MAX_INCIDENTS:], "mismatch_polls": self.mismatch_polls,
                "emergency": self.emergency, "ledger_adj": self.ledger_adj, "facts": self.facts,
                "halt_reason": self.halt_reason}

    def _persist(self) -> None:
        self.store.save(self.to_state())

    def _incident(self, kind: str, detail: str, t_ms: int | None = None, **extra: Any) -> None:
        self.incidents.append({"kind": kind, "detail": detail[:300], "t_ms": t_ms, **extra})
        self.log.critical("LSRL_LIVE_INCIDENT | %s | %s", kind, detail[:300])

    def _halt(self, reason: str) -> None:
        if self.halt_reason is None:
            self.halt_reason = reason
            self.log.critical("LSRL_LIVE_HALT | reason=%s | no new entries; protection continues", reason)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hold(direction: Direction | str) -> str:
        return "long" if str(getattr(direction, "value", direction)).upper() == "LONG" else "short"

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
        """Central risk authorization, recorded from the evaluated verdict and then REQUIRED."""
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

    def _clock_gate(self) -> None:
        """Refuse BEFORE any intent is written: a raw ClockUnsafe from inside the submitter would be
        classified 'unclassified, may exist' and churn the intent store for an order never sent."""
        gate = getattr(self.client, "_require_safe_clock_for_order", None)
        if gate is not None:
            gate()

    # ------------------------------------------------------------------ exchange reads
    def _read_positions(self) -> dict[str, float]:
        """hold side -> size on OUR symbol. Raises when the state cannot be established (never guesses)."""
        payload = self.client.get_all_positions()
        out: dict[str, float] = {"long": 0.0, "short": 0.0}
        for row in (payload or {}).get("data") or []:
            if str(row.get("symbol") or "").upper() != self.rules.symbol:
                continue
            side = str(row.get("holdSide") or "").lower()
            if side in out:
                out[side] += _f(row.get("total") or row.get("size"))
        return out

    def _mark(self) -> float | None:
        try:
            for row in (self.client.get_all_positions() or {}).get("data") or []:
                if str(row.get("symbol") or "").upper() == self.rules.symbol and _f(row.get("markPrice")) > 0:
                    return _f(row.get("markPrice"))
        except Exception:                                   # noqa: BLE001
            return None
        return None

    def _ensure_facts(self) -> None:
        if self._facts_ok:
            return
        try:
            facts = self._verify_facts(self.client, symbol=self.rules.symbol, product_type=self.product_type,
                                       expected=self.rules)
        except ExchangeFactsError as exc:
            self._incident("EXCHANGE_FACTS_UNVERIFIED", str(exc))
            self._halt(f"EXCHANGE_FACTS_UNVERIFIED:{exc}"[:200])
            raise LiveExecutorError(str(exc)) from exc
        except Exception as exc:                            # noqa: BLE001
            self._incident("EXCHANGE_FACTS_UNVERIFIED", _reason(exc))
            self._halt(f"EXCHANGE_FACTS_UNVERIFIED:{_reason(exc)}")
            raise LiveExecutorError(_reason(exc)) from exc
        self.facts = facts.to_dict()
        self._facts_ok = True

    # ------------------------------------------------------------------ setup registry
    def _setup(self, setup_id: str, direction: Direction | str) -> dict[str, Any]:
        rec = self.setups.get(setup_id)
        if rec is None:
            rec = {"setup_id": setup_id, "direction": str(getattr(direction, "value", direction)).upper(),
                   "hold": self._hold(direction), "opened_ms": None, "engine_closed": False, "closed_ms": None,
                   "stop_price": None, "desired_stop_qty": 0.0, "desired_targets": {}, "tp_version": 0,
                   "tp_fail": {}, "leverage": None, "plan_id": "", "candidate_id": "", "first_entry_order_id": "",
                   "first_entry_client_oid": "", "claimed": False, "finalized": False, "preflight_ok": False}
            self.setups[setup_id] = rec
        return rec

    def _orders_of(self, setup_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.orders.values()
                if r["setup_id"] == setup_id and (kind is None or r["kind"] == kind)]

    def _ledger(self, hold: str) -> float:
        """Units we hold on `hold`, from EXCHANGE-CONFIRMED fills only: entered - exited."""
        total = 0.0
        for rec in self.orders.values():
            if self.setups.get(rec["setup_id"], {}).get("hold") != hold:
                continue
            total += rec["reported_qty"] if rec["kind"] == "ENTRY" else -rec["reported_qty"]
        for agg in self.stop_fills.values():
            if agg.get("hold") == hold:
                total -= agg["qty"]
        return total + self.ledger_adj.get(hold, 0.0)

    # ------------------------------------------------------------------ intents
    def apply(self, intents: list[Intent], t_ms: int) -> list[Any]:
        events: list[Any] = []
        assert_armed(self.settings)                       # re-checked on EVERY apply (kill switch)
        blocked: str | None = None
        entry_batch = [i for i in intents if isinstance(i, PlaceEntry)]
        if entry_batch:
            blocked = self._entry_cycle_gate(entry_batch[0], t_ms)
        for it in intents:
            try:
                if isinstance(it, PlaceEntry):
                    if blocked:
                        events.append(OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", blocked))
                    else:
                        events += self._place_entry(it, t_ms)
                elif isinstance(it, CancelOrder):
                    self._cancel(it, t_ms)
                elif isinstance(it, SetStop):
                    events += self._set_stop(it, t_ms)
                elif isinstance(it, SetTargets):
                    events += self._set_targets(it, t_ms)
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
            return self._protection_failed(it.setup_id, it.direction, reason, t_ms)
        if isinstance(it, SetTargets):
            self._incident("TARGETS_FAILED", reason, t_ms)
            self._halt(f"MANAGEMENT_FAILED:{reason}")
            return []
        if isinstance(it, CancelOrder):
            self._incident("CANCEL_FAILED", reason, t_ms, tag=it.tag)
            self._halt(f"MANAGEMENT_FAILED:{reason}")
        return []

    # ------------------------------------------------------------------ entry cycle
    def _entry_cycle_gate(self, first: PlaceEntry, t_ms: int) -> str | None:
        """Runs ONCE before a setup's ladder is sent. Returns a rejection reason (or None)."""
        if self.halt_reason:
            return f"EXECUTOR_HALTED:{self.halt_reason}"
        if first.setup_id in self.setups and self.setups[first.setup_id].get("claimed"):
            return None                                     # restart / duplicate delivery of a known setup
        try:
            self._ensure_facts()
        except LiveExecutorError as exc:
            return f"EXCHANGE_FACTS_UNVERIFIED:{exc}"[:200]
        if self.pending_central:
            return "CENTRAL_CLOSE_RECORD_PENDING"
        hold = self._hold(first.direction)
        try:
            positions = self._read_positions()
        except Exception as exc:                            # noqa: BLE001
            self._incident("POSITION_STATE_UNKNOWN", _reason(exc), t_ms)
            self._halt(f"POSITION_STATE_UNKNOWN:{_reason(exc)}")
            return f"POSITION_STATE_UNKNOWN:{_reason(exc)}"[:200]
        exposure = sum(positions.values())
        if exposure > EPS:
            self._incident("FOREIGN_POSITION_PRESENT", f"positions={positions}", t_ms)
            self._halt("FOREIGN_POSITION_PRESENT")
            return "FOREIGN_POSITION_PRESENT"
        try:
            foreign = [r for r in _rows(self.client.get_pending_orders(symbol=self.rules.symbol))
                       if str(r.get("clientOid") or "") not in self.orders]
        except Exception as exc:                            # noqa: BLE001
            self._incident("PENDING_ORDERS_UNREADABLE", _reason(exc), t_ms)
            return f"PENDING_ORDERS_UNREADABLE:{_reason(exc)}"[:200]
        if foreign:                                         # not a halt: they may be a transient legacy order
            self._incident("FOREIGN_ORDERS_PRESENT", f"{len(foreign)} resting order(s) on {self.rules.symbol} "
                           "are not LSRL's", t_ms)
            return "FOREIGN_ORDERS_PRESENT"
        try:
            self.ownership.claim(self.rules.symbol, hold, STRATEGY_ID, first.setup_id)
        except (OwnershipConflict, OwnershipUnreadable) as exc:
            self._incident("OWNERSHIP_REFUSED", str(exc), t_ms)
            self._halt(f"OWNERSHIP_REFUSED:{exc}"[:200])
            return f"OWNERSHIP_REFUSED:{exc}"[:200]
        setup = self._setup(first.setup_id, first.direction)
        setup["claimed"] = True
        return None

    def _place_entry(self, it: PlaceEntry, t_ms: int) -> list[Any]:
        if any(r["tag"] == it.tag and r["kind"] == "ENTRY" for r in self.orders.values()):
            return []                                       # restart / duplicate delivery: already placed
        if self.halt_reason:
            return [OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", f"EXECUTOR_HALTED:{self.halt_reason}")]
        setup = self._setup(it.setup_id, it.direction)
        plan, ident = self._rung_plan(it)
        self._authorize(plan)                              # 1. mandatory central risk gate
        self._clock_gate()                                 # 2. clock gate, checked up front (also inside the client)
        self._preflight(it, setup)                         # 3. leverage
        result = submit_persistent_maker_entry(            # 4. audited boundary
            submitter=self.submitter, client=self.client, plan=ident, size=it.qty, price=it.price,
            notional_usdt=plan.position_notional_usdt, direction=it.direction.value)
        setup["plan_id"] = setup["plan_id"] or plan.plan_id
        setup["candidate_id"] = setup["candidate_id"] or plan.candidate_id
        setup["first_entry_order_id"] = setup["first_entry_order_id"] or result.order_id
        setup["first_entry_client_oid"] = setup["first_entry_client_oid"] or result.client_oid
        self.orders[result.client_oid] = self._order_rec(it.tag, "ENTRY", it.setup_id, it.rung, result.client_oid,
                                                         result.order_id, it.price, it.qty, it.direction.value, t_ms)
        return [OrderEvent(t_ms, it.setup_id, it.tag, "ACCEPTED")]

    @staticmethod
    def _order_rec(tag: str, kind: str, setup_id: str, rung: int | None, client_oid: str, order_id: str,
                   price: float, qty: float, direction: str, t_ms: int, **extra: Any) -> dict[str, Any]:
        rec = {"tag": tag, "kind": kind, "setup_id": setup_id, "rung": rung, "client_oid": client_oid,
               "order_id": order_id, "price": price, "qty": qty, "direction": direction, "reported_qty": 0.0,
               "reported_notional": 0.0, "reported_fee": 0.0, "terminal": False, "terminal_state": "",
               "cancel_requested": False, "superseded": False, "cancel_event_pending": False,
               "lost": False, "created_ms": t_ms, "first_fill_ms": None}
        rec.update(extra)
        return rec

    def _preflight(self, it: PlaceEntry, setup: dict[str, Any]) -> None:
        if setup.get("preflight_ok"):
            return
        verdict, _c = self._verdict(it.setup_id)
        self.client.set_futures_leverage(self.rules.symbol, leverage=max(1, int(verdict.leverage)),
                                         hold_side=self._hold(it.direction), margin_mode="isolated")
        setup["leverage"] = verdict.leverage
        setup["preflight_ok"] = True

    # ------------------------------------------------------------------ cancel = a REQUEST
    def _cancel(self, it: CancelOrder, t_ms: int) -> None:
        for rec in self._orders_of(it.setup_id):
            if rec["tag"] != it.tag or rec["terminal"] or rec["kind"] != "ENTRY":
                continue
            rec["cancel_requested"] = True
            try:
                self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=rec["client_oid"])
            except Exception as exc:                        # noqa: BLE001
                text = str(exc).lower()
                if "not exist" in text or "no such" in text or "43001" in text:
                    continue                                # filled or gone: the poll reads the truth
                raise

    # ------------------------------------------------------------------ stop = VERIFIED protection
    def _plan_rows(self) -> list[dict[str, Any]]:
        return _rows(self.client.get_tpsl_orders(product_type=self.product_type, symbol=self.rules.symbol,
                                                 plan_type="profit_loss"))

    def _stop_result_verified(self, res: Any) -> tuple[bool, str]:
        if not isinstance(res, dict):
            return False, f"stop client returned {type(res).__name__}"
        if res.get("verified") is not True:
            return False, str(res.get("reason_detail") or res.get("reason") or "client verified!=True")
        return True, ""

    def _verify_stop(self, hold: str, price: float, live_qty: float | None = None) -> tuple[bool, str]:
        """INDEPENDENT read-back: exactly the stop we intended, on the right side, covering the position
        that EXISTS on the exchange (the client sizes a loss plan to the live position; a fill still in
        flight is picked up by the next audit, never assumed)."""
        try:
            qty = self._read_positions()[hold] if live_qty is None else live_qty
            rows = self._plan_rows()
        except Exception as exc:                            # noqa: BLE001
            return False, f"PLAN_READ_FAILED:{_reason(exc)}"
        if qty <= self.rules.qty_step / 2:
            return True, ""                                 # nothing left to protect
        losses = []
        for row in rows:
            ptype = str(row.get("planType") or row.get("orderType") or "").lower()
            if "loss" not in ptype:
                continue
            side = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if side and side != hold:
                continue
            losses.append((ptype, row))
        tol = 2 * self.rules.tick_size
        match = [(t, r) for t, r in losses
                 if abs(_f(r.get("triggerPrice") or r.get("planTriggerPrice") or r.get("stopLossTriggerPrice"), -1) - price) <= tol]
        if not match:
            return False, "NO_MATCHING_STOP_PLAN"
        if len(losses) != len(match):
            return False, "EXTRA_STOP_PLANS"
        if any(t.startswith("pos_") for t, _ in match):
            return True, ""                                 # position-level stop covers the whole position
        covered = sum(_f(r.get("size")) for _, r in match)
        if covered + self.rules.qty_step / 2 < qty:
            return False, f"STOP_SIZE_INSUFFICIENT:covered={covered} need={qty}"
        return True, ""

    def _install_stop(self, setup: dict[str, Any], t_ms: int, reason: str) -> None:
        """Place + verify. Raises when protection cannot be PROVEN (caller flattens)."""
        hold, price, qty = setup["hold"], setup["stop_price"], setup["desired_stop_qty"]
        last: str = "unknown"
        for _attempt in range(STOP_RETRIES):
            try:
                res = self.client.move_futures_stop_loss(
                    symbol=self.rules.symbol, hold_side=hold, trigger_price=price,
                    direction=setup["direction"], cleanup_existing=True, reason=reason)
                ok, why = self._stop_result_verified(res)
                if ok:
                    ok, why = self._verify_stop(hold, price)
                if ok:
                    self.stop = {"setup_id": setup["setup_id"], "price": price, "qty": qty,
                                 "direction": setup["direction"], "hold": hold, "armed_ms": t_ms,
                                 "verified_ms": t_ms}
                    return
                last = why
            except Exception as exc:                        # noqa: BLE001
                last = _reason(exc)
            self._sleep(1.0)
        raise LiveExecutorError(f"STOP_NOT_VERIFIED:{last}")

    def _set_stop(self, it: SetStop, t_ms: int) -> list[Any]:
        setup = self._setup(it.setup_id, it.direction)
        if it.qty <= 0:
            return self._remove_stop(setup, t_ms)
        setup["stop_price"], setup["desired_stop_qty"] = it.price, it.qty
        self._install_stop(setup, t_ms, it.reason)
        return []

    def _remove_stop(self, setup: dict[str, Any], t_ms: int) -> list[Any]:
        """The engine says the setup is flat. Never remove protection from exposure that still exists."""
        setup["engine_closed"] = True
        setup["closed_ms"] = t_ms
        try:
            positions = self._read_positions()
        except Exception as exc:                            # noqa: BLE001
            self._incident("POSITION_STATE_UNKNOWN", _reason(exc), t_ms)
            self._halt(f"POSITION_STATE_UNKNOWN:{_reason(exc)}")
            return []                                       # stop stays: state unknown
        if positions[setup["hold"]] > self.rules.qty_step / 2:
            self._incident("STOP_REMOVAL_REFUSED", f"engine closed {setup['setup_id']} but the exchange still "
                           f"holds {positions[setup['hold']]}", t_ms)
            return self._orphan_exposure(setup["setup_id"], setup["direction"], "ENGINE_CLOSED_WITH_EXPOSURE", t_ms)
        try:
            self.client.cancel_all_futures_tpsl_orders(symbol=self.rules.symbol, hold_side=setup["hold"])
        except Exception as exc:                            # noqa: BLE001
            self._incident("STOP_CLEANUP_FAILED", _reason(exc), t_ms)
        if self.stop and self.stop["setup_id"] == setup["setup_id"]:
            self.stop = None
        return []

    # ------------------------------------------------------------------ emergency handling
    def _cancel_own_resting(self, setup_id: str) -> None:
        """Resting reduce-only take-profits OCCUPY the position (Bitget hedge mode): a market close is
        sized against what is left after them. Cancel OUR orders only (never anyone else's)."""
        for rec in self._orders_of(setup_id):
            if rec["terminal"] or rec["cancel_requested"]:
                continue
            rec["cancel_requested"] = True
            try:
                self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=rec["client_oid"])
            except Exception as exc:                        # noqa: BLE001
                if not ("not exist" in str(exc).lower() or "43001" in str(exc)):
                    self._incident("EMERGENCY_CANCEL_FAILED", _reason(exc), tag=rec["tag"])

    def _emergency_flatten(self, setup_id: str, direction: str, reason: str, t_ms: int) -> list[Any]:
        """Close OUR exposure at market and PROVE it. Anything short of proven-flat stays an open
        emergency that every poll retries; the executor is halted throughout."""
        self._halt(reason[:200])
        self._cancel_own_resting(setup_id)
        hold = self._hold(direction)
        try:
            result = self.client.close_futures_position_full(
                symbol=self.rules.symbol, direction=str(direction).upper(), margin_mode="isolated",
                reason="lsrl_unprotected_position", cleanup_tpsl=False)
        except Exception as exc:                            # noqa: BLE001
            self.emergency = {"setup_id": setup_id, "direction": str(direction).upper(), "reason": reason,
                              "attempts": (self.emergency or {}).get("attempts", 0) + 1, "error": _reason(exc)}
            self._incident("EMERGENCY_CLOSE_FAILED", _reason(exc), t_ms)
            return []
        status = str((result or {}).get("status") or "").upper()
        try:
            remaining = self._read_positions()[hold]
        except Exception as exc:                            # noqa: BLE001
            self.emergency = {"setup_id": setup_id, "direction": str(direction).upper(), "reason": reason,
                              "attempts": (self.emergency or {}).get("attempts", 0) + 1,
                              "error": f"POSITION_READ:{_reason(exc)}"}
            return []
        if status in ("CLOSED", "NO_POSITION") and remaining <= self.rules.qty_step / 2:
            ledger = max(0.0, self._ledger(hold))
            self.ledger_adj[hold] = self.ledger_adj.get(hold, 0.0) - ledger
            self.emergency = None
            self._incident("EMERGENCY_FLATTENED", f"{reason}; closed={ledger}", t_ms)
            if ledger <= EPS:
                return []
            price = _f((result or {}).get("avg_price") or (result or {}).get("price")) or (self._mark() or 0.0)
            return [FillEvent(t_ms, setup_id, f"lsrl-{setup_id}-emergency", "OTHER_CLOSE", price, ledger,
                              liquidity="taker")]
        self.emergency = {"setup_id": setup_id, "direction": str(direction).upper(), "reason": reason,
                          "attempts": (self.emergency or {}).get("attempts", 0) + 1,
                          "error": f"status={status} remaining={remaining}"}
        self._incident("EMERGENCY_CLOSE_INCOMPLETE", f"status={status} remaining={remaining}", t_ms)
        return []

    def _protection_failed(self, setup_id: str, direction: Any, reason: str, t_ms: int) -> list[Any]:
        """A position whose stop cannot be PROVEN is flattened: never knowingly unprotected."""
        self._incident("PROTECTION_FAILED", reason, t_ms, setup_id=setup_id)
        return self._emergency_flatten(setup_id, str(getattr(direction, "value", direction)),
                                       f"STOP_PLACEMENT_FAILED:{reason}", t_ms)

    def _orphan_exposure(self, setup_id: str, direction: str, why: str, t_ms: int) -> list[Any]:
        """Exposure our engine no longer manages: protect-or-flatten, then halt (no silent adoption)."""
        self._incident("ORPHAN_EXPOSURE", why, t_ms, setup_id=setup_id)
        return self._emergency_flatten(setup_id, direction, f"ORPHAN_EXPOSURE:{why}", t_ms)

    # ------------------------------------------------------------------ targets
    def _set_targets(self, it: SetTargets, t_ms: int) -> list[Any]:
        setup = self._setup(it.setup_id, it.direction)
        desired = {t.level: t for t in it.targets}
        setup["desired_targets"] = {lvl: {"tag": t.tag, "price": t.price, "qty": t.qty} for lvl, t in desired.items()}
        for rec in self._orders_of(it.setup_id):
            if rec["kind"] not in ("TP1", "TP2", "TP3") or rec["terminal"] or rec["cancel_requested"]:
                continue
            want = desired.get(rec["kind"])
            if want is None or abs(want.price - rec["price"]) > EPS or abs(want.qty - rec["level_qty"]) > EPS:
                self._cancel_tp(rec)
        self._ensure_targets(setup, t_ms)
        return []

    def _cancel_tp(self, rec: dict[str, Any]) -> None:
        rec["cancel_requested"] = True
        rec["superseded"] = True                            # keep POLLING it: a late fill must still be reported
        try:
            self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=rec["client_oid"])
        except Exception as exc:                            # noqa: BLE001
            if not ("not exist" in str(exc).lower() or "43001" in str(exc)):
                raise

    def _tp_filled_total(self, setup_id: str, level: str) -> float:
        return sum(r["reported_qty"] for r in self._orders_of(setup_id, level))

    def _ensure_targets(self, setup: dict[str, Any], t_ms: int) -> None:
        """Every desired take-profit is either live, complete, or (re-)placed for the unfilled remainder."""
        if setup["engine_closed"] or self.emergency:
            return
        for level, want in sorted(setup["desired_targets"].items()):
            live = [r for r in self._orders_of(setup["setup_id"], level)
                    if not r["terminal"] and not r["superseded"] and not r["cancel_requested"]]
            if live:
                continue
            remaining = want["qty"] - self._tp_filled_total(setup["setup_id"], level)
            remaining = round(remaining / self.rules.qty_step + 1e-9) * self.rules.qty_step
            if remaining < self.rules.min_qty - EPS:
                continue                                    # level complete (or dust the exchange cannot take)
            if setup["tp_fail"].get(level, 0) >= TP_PLACE_ATTEMPTS:
                continue
            setup["tp_version"] += 1
            oid = f"lsrl{setup['setup_id'][:10]}{level.lower()}v{setup['tp_version']}"
            try:
                res = self.client.place_futures_limit_close_order(
                    symbol=self.rules.symbol, hold_side=setup["hold"], size=remaining, price=want["price"],
                    client_oid=oid, post_only=True)
            except Exception as exc:                        # noqa: BLE001
                setup["tp_fail"][level] = setup["tp_fail"].get(level, 0) + 1
                self.log.warning("LSRL_LIVE_TP_PLACE_FAILED | %s | %s | attempt=%s | %s", setup["setup_id"], level,
                                 setup["tp_fail"][level], _reason(exc))
                if setup["tp_fail"][level] >= TP_PLACE_ATTEMPTS:
                    self._incident("TP_CANNOT_BE_PLACED", f"{level}: {_reason(exc)}", t_ms)
                    self._halt(f"MANAGEMENT_FAILED:TP_CANNOT_BE_PLACED:{level}")
                continue
            setup["tp_fail"][level] = 0
            self.orders[oid] = self._order_rec(
                want["tag"], level, setup["setup_id"], None, oid,
                str(((res or {}).get("data") or {}).get("orderId") or ""), want["price"], remaining,
                setup["direction"], t_ms, level_qty=want["qty"])

    # ------------------------------------------------------------------ reconciliation
    def _pending_map(self) -> dict[str, dict[str, Any]] | None:
        """ONE read for every resting order; None = unreadable (callers fall back to per-order lookups)."""
        try:
            rows = _rows(self.client.get_pending_orders(symbol=self.rules.symbol))
        except Exception:                                   # noqa: BLE001
            return None
        return {str(r.get("clientOid") or ""): r for r in rows if r.get("clientOid")}

    def _resolve(self, rec: dict[str, Any], pending: dict[str, dict[str, Any]] | None) -> tuple[str, dict | None]:
        if pending is not None and rec["client_oid"] in pending:
            return "FOUND", pending[rec["client_oid"]]
        found = self.client.find_order_by_client_oid(self.rules.symbol, rec["client_oid"])
        return str(found.get("status")), found.get("order")

    def _reconcile_order(self, rec: dict[str, Any], pending: dict[str, dict[str, Any]] | None, t_ms: int
                         ) -> list[Any]:
        events: list[Any] = []
        status, row = self._resolve(rec, pending)
        if status == "UNKNOWN":
            self._incident("ORDER_STATE_UNKNOWN", rec["client_oid"], t_ms)
            self._halt(f"ORDER_STATE_UNKNOWN:{rec['client_oid']}")
            return events
        if status == "ABSENT":
            rec["terminal"], rec["terminal_state"] = True, "absent"
            self._incident("ORDER_ABSENT", rec["client_oid"], t_ms)
            self._halt(f"ORDER_ABSENT:{rec['client_oid']}")
            if rec["kind"] == "ENTRY":
                events.append(OrderEvent(t_ms, rec["setup_id"], rec["tag"], "CANCELLED", "ABSENT_ON_EXCHANGE"))
            elif not rec["cancel_requested"]:
                rec["lost"] = True                          # _ensure_targets re-places the unfilled remainder
            return events
        metrics = self.client.extract_fill_metrics({"data": row or {}})
        filled, avg = _f(metrics["filled_qty"]), _f(metrics["avg_price"])
        fee = abs(_f(metrics["fee"]))
        if filled + EPS < rec["reported_qty"]:              # cumulative fills never go DOWN: keep what we reported
            self._incident("FILL_REGRESSION", f"{rec['client_oid']} filled={filled} reported={rec['reported_qty']}",
                           t_ms)
            filled = rec["reported_qty"]
        elif filled > rec["reported_qty"] + EPS:
            delta = filled - rec["reported_qty"]
            notional = filled * avg - rec["reported_notional"]
            price = notional / delta if delta > EPS and avg > 0 else rec["price"]
            events.append(FillEvent(
                t_ms, rec["setup_id"], rec["tag"], rec["kind"], price, delta,
                fee_usdt=max(0.0, fee - rec["reported_fee"]), liquidity="maker", rung=rec.get("rung")))
            rec["reported_qty"], rec["reported_notional"], rec["reported_fee"] = filled, filled * avg, fee
            if rec["first_fill_ms"] is None:
                rec["first_fill_ms"] = t_ms
            setup = self.setups.get(rec["setup_id"])
            if setup is not None and rec["kind"] == "ENTRY" and setup["opened_ms"] is None:
                setup["opened_ms"] = t_ms
            if setup is not None and rec["kind"] == "ENTRY" and setup["engine_closed"]:
                setup["orphan_qty"] = setup.get("orphan_qty", 0.0) + delta
        state = str(metrics["state"]).lower()
        if state in _TERMINAL:
            rec["terminal"], rec["terminal_state"] = True, state
            if state not in _FILLED:
                cancel_reason = str((row or {}).get("cancelReason") or "")
                if rec["kind"] == "ENTRY":
                    rec["cancel_event_pending"] = True      # only after the final fill + position reconciliation
                    rec["cancel_reason"] = state if not cancel_reason else f"{state}:{cancel_reason}"
                elif not rec["cancel_requested"] and rec["reported_qty"] + EPS < rec["qty"]:
                    rec["lost"] = True                      # the exchange dropped a take-profit we did not cancel
                    self._incident("TP_LOST", f"{rec['kind']} {rec['client_oid']} state={state} "
                                   f"cancelReason={cancel_reason or '-'} filled={rec['reported_qty']}/{rec['qty']}", t_ms)
        return events

    def _stop_fill_events(self, t_ms: int) -> list[Any]:
        """Stop fills come from the exchange ORDER HISTORY (real price and fee), keyed by orderId with
        cumulative quantity: a position shrinking between two reads is never read as a stop."""
        if not self.stop:
            return []
        stop = self.stop
        try:
            rows = _rows(self.client.get_order_history(product_type=self.product_type, symbol=self.rules.symbol,
                                                       limit=100))
        except Exception as exc:                            # noqa: BLE001
            self._incident("STOP_FILL_HISTORY_UNREADABLE", _reason(exc), t_ms)
            return []
        events: list[Any] = []
        for row in rows:
            source = str(row.get("orderSource") or "").lower()
            if source not in _STOP_SOURCES:
                continue
            side = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if side and side != stop["hold"]:
                continue
            if int(_f(row.get("cTime"))) + 5_000 < stop["armed_ms"]:
                continue
            if str(row.get("status") or row.get("state") or "").lower() not in ("filled", "partially_filled"):
                continue
            oid = str(row.get("orderId") or "")
            filled, avg, fee = _f(row.get("baseVolume")), _f(row.get("priceAvg")), abs(_f(row.get("fee")))
            seen = self.stop_fills.setdefault(oid, {"qty": 0.0, "notional": 0.0, "fee": 0.0, "hold": stop["hold"],
                                                    "setup_id": stop["setup_id"]})
            if filled > seen["qty"] + EPS:
                delta = filled - seen["qty"]
                price = (filled * avg - seen["notional"]) / delta if delta > EPS and avg > 0 else stop["price"]
                events.append(FillEvent(t_ms, stop["setup_id"], f"lsrl-{stop['setup_id']}-stop", "SL", price, delta,
                                        fee_usdt=max(0.0, fee - seen["fee"]), liquidity="taker"))
                seen["qty"], seen["notional"], seen["fee"] = filled, filled * avg, fee
        return events

    def _reconcile_orders(self, t_ms: int) -> list[Any]:
        events: list[Any] = []
        pending = self._pending_map()
        for rec in list(self.orders.values()):
            if not rec["terminal"]:
                events += self._reconcile_order(rec, pending, t_ms)
        events += self._stop_fill_events(t_ms)
        return events

    def _exposure_check(self, t_ms: int, events: list[Any]) -> tuple[bool, dict[str, float] | None]:
        """Ledger (exchange-confirmed fills) vs exchange position. A one-poll difference can be a fill in
        flight between two reads, so the check is repeated (orders re-read first) before it counts."""
        positions: dict[str, float] | None = None
        for round_ in range(MISMATCH_ROUNDS):
            try:
                positions = self._read_positions()
            except Exception as exc:                        # noqa: BLE001
                self._incident("POSITION_STATE_UNKNOWN", _reason(exc), t_ms)
                self._halt(f"POSITION_STATE_UNKNOWN:{_reason(exc)}")
                return False, None
            bad = {h: (positions[h], self._ledger(h)) for h in positions
                   if abs(positions[h] - self._ledger(h)) > self.rules.qty_step / 2
                   and (positions[h] > EPS or self._ledger(h) > EPS)}
            if not bad:
                self.mismatch_polls = 0
                return True, positions
            if round_ + 1 < MISMATCH_ROUNDS:
                self._sleep(0.25)
                events += self._reconcile_orders(t_ms)      # a fill in flight shows up in the order re-read
        self.mismatch_polls += 1
        for hold, (exchange_qty, ledger_qty) in bad.items():
            if exchange_qty > ledger_qty + self.rules.qty_step / 2:
                self._incident("UNEXPLAINED_EXPOSURE", f"{hold}: exchange={exchange_qty} ledger={ledger_qty}", t_ms)
                self._halt("FOREIGN_POSITION_PRESENT" if ledger_qty <= EPS else
                           f"UNEXPLAINED_EXPOSURE:{hold}:{exchange_qty}>{ledger_qty}")
            else:
                self._incident("LEDGER_EXCEEDS_EXCHANGE", f"{hold}: exchange={exchange_qty} ledger={ledger_qty}", t_ms)
                self._halt(f"LEDGER_EXCEEDS_EXCHANGE:{hold}:{exchange_qty}<{ledger_qty}")
        return False, positions

    def _audit_protection(self, positions: dict[str, float], t_ms: int) -> list[Any]:
        """Every poll: exposure we own has a PROVEN stop, or it is repaired, or it is flattened."""
        events: list[Any] = []
        for sid, setup in self.setups.items():
            if setup["engine_closed"] or setup["stop_price"] is None or setup["desired_stop_qty"] <= 0:
                continue
            if positions.get(setup["hold"], 0.0) <= self.rules.qty_step / 2:
                continue
            ok, why = self._verify_stop(setup["hold"], setup["stop_price"], positions[setup["hold"]])
            if ok:
                if self.stop:
                    self.stop["verified_ms"] = t_ms
                continue
            self._incident("STOP_AUDIT_FAILED", why, t_ms, setup_id=sid)
            try:
                self._install_stop(setup, t_ms, "AUDIT_REPAIR")
            except Exception as exc:                        # noqa: BLE001
                events += self._protection_failed(sid, setup["direction"], _reason(exc), t_ms)
        return events

    # ------------------------------------------------------------------ poll
    def poll(self, t_ms: int) -> list[Any]:
        """Read the exchange and translate reality into engine events. Never adds exposure."""
        events: list[Any] = []
        active = (any(not r["terminal"] for r in self.orders.values()) or self.stop or self.emergency
                  or self.pending_central or any(not s["finalized"] for s in self.setups.values()))
        if not active:
            return events
        if self.emergency:
            events += self._retry_emergency(t_ms)
        events += self._reconcile_orders(t_ms)
        for setup in self.setups.values():
            if setup.get("orphan_qty", 0.0) > EPS and not self.emergency:
                qty, setup["orphan_qty"] = setup["orphan_qty"], 0.0
                events += self._orphan_exposure(setup["setup_id"], setup["direction"],
                                                f"ENTRY_FILLED_AFTER_ENGINE_CLOSE:{qty}", t_ms)
        consistent, positions = self._exposure_check(t_ms, events)
        if positions is not None and consistent and not self.emergency:
            for hold, qty in positions.items():
                if qty > self.rules.qty_step / 2 and not any(
                        not s["engine_closed"] and s["hold"] == hold for s in self.setups.values()):
                    sid = next((s["setup_id"] for s in self.setups.values() if s["hold"] == hold), "")
                    events += self._orphan_exposure(sid, "LONG" if hold == "long" else "SHORT",
                                                    f"EXPOSURE_WITHOUT_ACTIVE_SETUP:{hold}:{qty}", t_ms)
        if positions is not None and not self.emergency:
            events += self._audit_protection(positions, t_ms)
            for setup in self.setups.values():
                if not setup["engine_closed"]:
                    self._ensure_targets(setup, t_ms)
        for rec in self.orders.values():                    # deferred cancel events: only when reconciled
            if rec.get("cancel_event_pending") and consistent:
                rec["cancel_event_pending"] = False
                events.append(OrderEvent(t_ms, rec["setup_id"], rec["tag"], "CANCELLED",
                                         str(rec.get("cancel_reason") or rec["terminal_state"])))
        events += self._advance_lifecycles(t_ms, positions)
        self._persist()
        return events

    def _retry_emergency(self, t_ms: int) -> list[Any]:
        em = self.emergency
        if em is None:
            return []
        if em.get("attempts", 0) >= EMERGENCY_MAX_ATTEMPTS:
            return []                                       # halted + incident already raised; owner action
        return self._emergency_flatten(em["setup_id"], em["direction"], em["reason"], t_ms)

    # ------------------------------------------------------------------ lifecycle / ownership / central record
    def _advance_lifecycles(self, t_ms: int, positions: dict[str, float] | None) -> list[Any]:
        for sid, setup in self.setups.items():
            if setup["finalized"] or self.emergency:
                continue
            if any(not r["terminal"] for r in self._orders_of(sid)):
                continue
            if positions is None or positions.get(setup["hold"], 0.0) > self.rules.qty_step / 2:
                continue
            if not setup["engine_closed"] and abs(self._ledger(setup["hold"])) > EPS:
                continue                                    # exposure expected but flat: reconciliation owns it
            setup["finalized"] = True
            entered = sum(r["reported_qty"] for r in self._orders_of(sid, "ENTRY"))
            if entered > EPS:
                self._queue_central(setup, t_ms)
            try:
                if not any(s["claimed"] and not s["finalized"] and s["hold"] == setup["hold"]
                           for s in self.setups.values()):
                    self.ownership.release(self.rules.symbol, setup["hold"], STRATEGY_ID)
            except (OwnershipConflict, OwnershipUnreadable) as exc:
                self._incident("OWNERSHIP_RELEASE_FAILED", str(exc), t_ms)
        self._advance_central(t_ms)
        return []

    def _queue_central(self, setup: dict[str, Any], t_ms: int) -> None:
        sid = setup["setup_id"]
        entries = self._orders_of(sid, "ENTRY")
        exits = [r for r in self.orders.values() if r["setup_id"] == sid and r["kind"] != "ENTRY"]
        stop_exit = [v for v in self.stop_fills.values() if v.get("setup_id") == sid]
        exit_qty = sum(r["reported_qty"] for r in exits) + sum(v["qty"] for v in stop_exit)
        exit_notional = sum(r["reported_notional"] for r in exits) + sum(v["notional"] for v in stop_exit)
        reason = ("STOP_LOSS" if stop_exit else
                  "TP3_FLAT" if any(r["kind"] == "TP3" and r["reported_qty"] > EPS for r in exits) else "CLOSED")
        life = {"setup_id": sid, "symbol": self.rules.symbol, "direction": setup["direction"],
                "entered_qty": sum(r["reported_qty"] for r in entries),
                "entry_notional": sum(r["reported_notional"] for r in entries),
                "exit_qty": exit_qty, "exit_notional": exit_notional,
                "opened_at_ms": setup["opened_ms"] or t_ms, "closed_at_ms": setup["closed_ms"] or t_ms,
                "stop_price": setup["stop_price"], "leverage": setup["leverage"], "plan_id": setup["plan_id"],
                "candidate_id": setup["candidate_id"], "first_entry_order_id": setup["first_entry_order_id"],
                "first_entry_client_oid": setup["first_entry_client_oid"], "close_reason": reason,
                "take_profits": [v["price"] for _l, v in sorted(setup["desired_targets"].items())]}
        position = self.central.position_record(life)
        self.pending_central[sid] = {"position": position, "close_reason": reason, "attempts": 0,
                                     "last_ms": 0, "provisional": False}

    def _advance_central(self, t_ms: int) -> None:
        for sid, item in list(self.pending_central.items()):
            if not item["provisional"]:
                try:
                    self.central.write_provisional(item["position"], item["close_reason"])
                    item["provisional"] = True
                except Exception as exc:                    # noqa: BLE001
                    self._incident("CENTRAL_PROVISIONAL_FAILED", _reason(exc), t_ms)
            if t_ms - item["last_ms"] < CENTRAL_RETRY_S * 1000 and item["attempts"] > 0:
                continue
            item["last_ms"] = t_ms
            outcome = self.central.reconcile_once(item["position"], item["close_reason"])
            item["attempts"] += 1
            if outcome in RECORDED:
                del self.pending_central[sid]
                self.log.warning("LSRL_CENTRAL_CLOSE_RECORDED | %s | %s", sid, outcome)
            elif item["attempts"] >= CENTRAL_MAX_ATTEMPTS:
                self._incident("CENTRAL_CLOSE_UNRECONCILED", f"{sid} outcome={outcome}", t_ms)
                self._halt(f"CENTRAL_CLOSE_RECORD_UNRECONCILED:{sid}")

    def resume(self) -> None:
        """Explicit owner action only; never called by the runtime."""
        self._incident("RESUME", f"halt cleared (was {self.halt_reason})")
        self.halt_reason = None
        self.emergency = None
        self._persist()
