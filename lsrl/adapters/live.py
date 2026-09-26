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
    `resume()` only); protection, cancellation and reconciliation continue;
  * THE EXPOSURE INVARIANT (`exposure` state machine, persisted, re-derived from the exchange on every poll and
    on every restart): any detected LSRL quantity converges to a VERIFIED protective stop or to VERIFIED FLAT and
    can never settle as 'unprotected + gave up'. The executor protects what it finds WITHOUT waiting for the engine
    (the frozen stop is recorded write-ahead with every entry), attempts protection, then a verified flatten, then
    keeps retrying both with bounded exponential backoff forever (`CRITICAL_UNPROTECTED_EXPOSURE`);
  * a fill the engine never acknowledges (its SetStop never covers the ledger) is flattened after a grace period;
  * entries carry a write-ahead record, so an order accepted just before a crash is found again on restart;
  * PROCESS-DOWN PROTECTION (`lsrl.adapters.process_down`): every resting entry carries the frozen protective stop as
    Bitget's order-level `presetStopLossPrice`, so a fill that happens while no process is alive is still stopped BY THE
    EXCHANGE. The executor cannot even be constructed without a verified attestation of how the exchange materialises
    that stop; an exchange-side stop is ADOPTED (never cancelled-then-replaced), and a stop that fires during an outage is
    read from the order history on restart so the engine and the central loss meters still learn of it.
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
from execution.order_identity import ENTRY_LEG_MAKER
from execution.strategy_ownership import (OwnershipConflict, OwnershipUnreadable,
                                          StrategyOwnershipRegistry, owner_family)
from lsrl.adapters.central_ledger import RECORDED, CentralCloseRecorder
from lsrl.adapters.process_down import ProcessDownProtection
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.contract import ExchangeFactsError, verify_exchange_facts
from lsrl.core.config import DEFAULT_CONFIG, STRATEGY_ID, LSRLConfig
from lsrl.core.types import (CancelOrder, Direction, FillEvent, Intent, OrderEvent, PlaceEntry, SetStop,
                             SetTargets, SymbolRules)
from risk.mandatory_authorization import (RiskAuthorizationError, RiskAuthorizationStore, record_authorization,
                                          require_authorized)

EPS = 1e-9
STATE_VERSION = 3
_TERMINAL = {"filled", "full_fill", "full-filled", "cancelled", "canceled", "rejected", "expired"}
_FILLED = {"filled", "full_fill", "full-filled"}
_STOP_SOURCES = ("loss_market", "loss_limit", "pos_loss_market", "pos_loss_limit")
STOP_RETRIES = 3
TP_PLACE_ATTEMPTS = 5
MISMATCH_ROUNDS = 3
CENTRAL_RETRY_S = 5.0
CENTRAL_MAX_ATTEMPTS = 120
MAX_INCIDENTS = 200
# Retry pacing is counted in POLLS (a poll is at most ~3 s apart, ~1 s while exposure is unresolved), not in caller-supplied
# timestamps: the retry never stops, the first three attempts are back to back, and the gap stops growing at 8 polls.
EXPOSURE_BACKOFF_POLLS = (0, 0, 0, 1, 2, 4, 8)
ENGINE_ACK_GRACE_MS = 20_000        # a filled quantity the engine has not covered with a SetStop by then is flattened
FAST_WATCH_MS = 1_000               # while an entry rests, look for its fill this often (the runtime poll is 3 s)
LOG_EVERY_N_ATTEMPTS = 10
EXPECTED_ABSENT_GRACE_POLLS = 5      # a write-ahead entry the exchange does not (yet) show is kept this many polls before it is dropped
EXPOSURE_NONE = "NONE"
EXPOSURE_PROTECTED = "PROTECTED"
EXPOSURE_UNPROTECTED = "UNPROTECTED_EXPOSURE"
EXPOSURE_CRITICAL = "CRITICAL_UNPROTECTED_EXPOSURE"


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
                 facts_verifier: Callable[..., Any] = verify_exchange_facts,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 fast_watch: bool = True,
                 process_down: ProcessDownProtection | None = None) -> None:
        assert_armed(settings)
        if not isinstance(process_down, ProcessDownProtection):
            raise LiveNotArmed("MICRO_LIVE not armed: PROCESS_DOWN_PROTECTION_NOT_ATTESTED")
        self.process_down = process_down
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
        self.exposure: dict[str, Any] = data.get("exposure") or self._fresh_exposure()
        self.latencies: list[dict[str, Any]] = data.get("latencies", [])
        self.startup_report: dict[str, Any] = data.get("startup_report") or {}
        self._facts_ok = False
        self._clock_ms = clock_ms
        self._fast_watch_enabled = fast_watch
        self._recovered = False
        self._verify_first = bool(data)      # after a restart a stop may already exist: read before placing another
        self._last_fast_ms = 0
        self._in_poll = False
        self._read_failures: dict[str, int] = {}
        self._unknown_orders = False
        self._polled_once = False
        self._immediate_failed_t: int | None = None
        self._immediate_failed_why = ""

    # ------------------------------------------------------------------ persistence / incidents
    def to_state(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, "orders": self.orders, "setups": self.setups, "stop": self.stop,
                "stop_fills": self.stop_fills, "pending_central": self.pending_central,
                "incidents": self.incidents[-MAX_INCIDENTS:], "mismatch_polls": self.mismatch_polls,
                "emergency": self.emergency, "ledger_adj": self.ledger_adj, "facts": self.facts,
                "halt_reason": self.halt_reason, "exposure": self.exposure, "latencies": self.latencies[-50:],
                "startup_report": self.startup_report}

    def _persist(self) -> None:
        self.store.save(self.to_state())

    @staticmethod
    def _fresh_exposure() -> dict[str, Any]:
        return {"state": EXPOSURE_NONE, "hold": None, "setup_id": None, "since_ms": None, "qty": 0.0, "attempts": 0,
                "wait": 0, "last_error": "", "recoveries": 0, "escalated": False}

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
                   "first_entry_client_oid": "", "claimed": False, "finalized": False, "preflight_ok": False,
                   "protect_price": None, "ack_qty": 0.0, "unacked_since_ms": None, "fill_seen_ms": None,
                   "fill_exchange_ms": None, "protected_ms": None, "expected_entries": {}}
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
        events += self._startup_reconcile(t_ms)
        events += self._fast_watch(t_ms)
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
            # order in an unknown state, so it halts NEW entries (the write-ahead record stays: restart
            # looks the clientOid up and adopts the order if it exists).
            if "status=REJECTED" not in reason:
                self._halt(f"ENTRY_FAILED:{reason}")
            else:
                expected = self._setup(it.setup_id, it.direction)["expected_entries"]
                for oid in [o for o, e in expected.items() if e["tag"] == it.tag]:
                    del expected[oid]
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
        if self.exposure["state"] in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL):
            return "UNPROTECTED_EXPOSURE"
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
        if self.exposure["state"] in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL):
            return [OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", "UNPROTECTED_EXPOSURE")]
        setup = self._setup(it.setup_id, it.direction)
        plan, ident = self._rung_plan(it)
        self._authorize(plan)                              # 1. mandatory central risk gate
        self._clock_gate()                                 # 2. clock gate, checked up front (also inside the client)
        self._preflight(it, setup)                         # 3. leverage
        # WRITE-AHEAD: before any order can exist on the exchange, persist the frozen protective stop and the
        # deterministic clientOid. A crash between the exchange accepting the order and the normal bookkeeping is
        # then recoverable: restart looks the order up by this clientOid instead of never knowing it existed.
        setup["protect_price"] = setup["protect_price"] or float(plan.stop_loss)
        if not setup["protect_price"] or setup["protect_price"] <= 0:
            # An entry that could rest WITHOUT its exchange-side stop is exactly the process-down hazard: never place it.
            return [OrderEvent(t_ms, it.setup_id, it.tag, "REJECTED", "PRESET_STOP_UNAVAILABLE")]
        expected_oid = self.submitter.client_oid_for(ident, leg=ENTRY_LEG_MAKER)
        setup["expected_entries"][expected_oid] = {"tag": it.tag, "rung": it.rung, "price": it.price, "qty": it.qty,
                                                   "direction": it.direction.value, "t_ms": t_ms}
        self._persist()
        result = submit_persistent_maker_entry(            # 4. audited boundary
            submitter=self.submitter, client=self.client, plan=ident, size=it.qty, price=it.price,
            notional_usdt=plan.position_notional_usdt, direction=it.direction.value,
            preset_stop_loss_price=setup["protect_price"])
        setup["plan_id"] = setup["plan_id"] or plan.plan_id
        setup["candidate_id"] = setup["candidate_id"] or plan.candidate_id
        setup["first_entry_order_id"] = setup["first_entry_order_id"] or result.order_id
        setup["first_entry_client_oid"] = setup["first_entry_client_oid"] or result.client_oid
        self.orders[result.client_oid] = self._order_rec(it.tag, "ENTRY", it.setup_id, it.rung, result.client_oid,
                                                         result.order_id, it.price, it.qty, it.direction.value, t_ms,
                                                         preset_stop=setup["protect_price"])
        setup["expected_entries"].pop(result.client_oid, None)
        setup["expected_entries"].pop(expected_oid, None)
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
        """Every plan order once. A row the exchange repeats (page overlap, a repeated response) must not count twice: coverage
        is a SUM of sizes, and a doubled row would make half a position look fully stopped."""
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for row in _rows(self.client.get_tpsl_orders(product_type=self.product_type, symbol=self.rules.symbol,
                                                     plan_type="profit_loss")):
            key = str(row.get("orderId") or row.get("planOrderId") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)
        return rows

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

    def _lone_position_level_stop(self, hold: str) -> bool:
        """The exchange holds a position-level stop and NO position-level take-profit: what an order-level preset stop
        becomes if Bitget materialises it that way. The shared client's position-level move refuses that (it must re-send
        a take-profit it does not have), so it needs its own mechanism."""
        try:
            rows = self._plan_rows()
        except Exception:                                   # noqa: BLE001 - unreadable: use the ordinary path
            return False
        loss = profit = False
        for row in rows:
            ptype = str(row.get("planType") or row.get("orderType") or "").lower()
            side = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if side and side != hold:
                continue
            loss = loss or ptype == "pos_loss"
            profit = profit or ptype == "pos_profit"
        return loss and not profit

    def _move_stop(self, setup: dict[str, Any], price: float, reason: str) -> Any:
        """One stop request on the mechanism that fits what the exchange actually holds. With a position-level attestation
        a lone position-level stop is rewritten in place (no gap); everything else is the shared, audited move."""
        hold = setup["hold"]
        if self.process_down.observed_model == "POSITION_LEVEL" and self._lone_position_level_stop(hold):
            return self.client.move_position_level_stop_only(
                symbol=self.rules.symbol, hold_side=hold, trigger_price=price, reason=reason)
        return self.client.move_futures_stop_loss(
            symbol=self.rules.symbol, hold_side=hold, trigger_price=price,
            direction=setup["direction"], cleanup_existing=True, reason=reason)

    def _install_stop(self, setup: dict[str, Any], t_ms: int, reason: str, *, price: float | None = None,
                      qty: float | None = None) -> None:
        """Place + verify. Raises when protection cannot be PROVEN (caller flattens)."""
        hold = setup["hold"]
        price = setup["stop_price"] if price is None else price
        qty = setup["desired_stop_qty"] if qty is None else qty
        last: str = "unknown"
        for _attempt in range(STOP_RETRIES):
            try:
                res = self._move_stop(setup, price, reason)
                ok, why = self._stop_result_verified(res)
                if ok:
                    ok, why = self._verify_stop(hold, price)
                if ok:
                    self.stop = {"setup_id": setup["setup_id"], "price": price, "qty": qty,
                                 "direction": setup["direction"], "hold": hold, "armed_ms": t_ms,
                                 "verified_ms": t_ms}
                    if setup["stop_price"] is None:         # protection placed before the engine asked for it
                        setup["stop_price"], setup["desired_stop_qty"] = price, qty
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
        setup["ack_qty"], setup["unacked_since_ms"] = it.qty, None        # the engine has seen the fills up to here
        if self.emergency:
            return []                                       # a verified flatten is already under way: no stop work
        if self._ledger(setup["hold"]) <= self.rules.qty_step / 2:
            return []                                       # our own books hold nothing (already flattened): nothing to protect
        if self._stop_already_in_place(setup):
            return []                                       # executor-first protection already placed exactly this
        self._install_stop(setup, t_ms, it.reason)
        return []

    def _stop_already_in_place(self, setup: dict[str, Any]) -> bool:
        """Moving a stop is cancel-then-place (a window with NO stop): never do it for a stop that is already
        there, exactly as asked, and independently proven on the exchange. That includes a stop the EXCHANGE created
        (preset stops of further rungs make the whole position covered without any request from us)."""
        cur = self.stop
        if cur and cur["setup_id"] != setup["setup_id"]:
            return False
        if (cur and abs(cur["price"] - setup["stop_price"]) <= self.rules.tick_size / 2
                and abs(cur["qty"] - setup["desired_stop_qty"]) <= self.rules.qty_step / 2):
            ok, _why = self._verify_stop(setup["hold"], setup["stop_price"], setup["desired_stop_qty"])
            if ok:
                cur["verified_ms"] = self._clock_ms()
            return ok
        return self._adopt_exchange_stop(setup, setup["stop_price"], setup["desired_stop_qty"], self._clock_ms())

    def _adopt_exchange_stop(self, setup: dict[str, Any], price: float | None, qty: float, t_ms: int) -> bool:
        """A stop that is ALREADY on the exchange (a preset stop that materialised at a fill) is adopted after the same
        independent read-back that proves our own stops. Replacing it would be cancel-then-place: a stop-less gap."""
        if price is None or qty <= self.rules.qty_step / 2:
            return False
        ok, _why = self._verify_stop(setup["hold"], price, qty)
        if not ok:
            return False
        self.stop = {"setup_id": setup["setup_id"], "price": price, "qty": qty, "direction": setup["direction"],
                     "hold": setup["hold"], "armed_ms": int(setup.get("fill_exchange_ms") or t_ms),
                     "verified_ms": t_ms, "adopted": True}
        if setup["stop_price"] is None:
            setup["stop_price"], setup["desired_stop_qty"] = price, qty
        return True

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

    def _cancel_pending_closes(self, hold: str) -> None:
        """Resting reduce-only closes on the side we are flattening occupy the position whether or not our state
        remembers placing them (a lost state file). The whole side is being closed, so they all go; OPEN orders
        that are not ours are never touched."""
        try:
            rows = _rows(self.client.get_pending_orders(symbol=self.rules.symbol))
        except Exception:                                   # noqa: BLE001
            return
        for row in rows:
            side = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if str(row.get("tradeSide") or "").lower() != "close" or (side and side != hold):
                continue
            ident = ({"client_oid": row["clientOid"]} if row.get("clientOid") else {"order_id": row.get("orderId")})
            try:
                self.client.cancel_futures_order(symbol=self.rules.symbol, **ident)
            except Exception as exc:                        # noqa: BLE001
                if not ("not exist" in str(exc).lower() or "43001" in str(exc)):
                    self._incident("EMERGENCY_CANCEL_FAILED", _reason(exc), tag=str(row.get("clientOid") or ""))

    @staticmethod
    def _gap_polls(attempts: int) -> int:
        return EXPOSURE_BACKOFF_POLLS[min(max(attempts, 1) - 1, len(EXPOSURE_BACKOFF_POLLS) - 1)]

    def _emergency_failed(self, setup_id: str, direction: str, reason: str, error: str, t_ms: int) -> list[Any]:
        """An emergency close that is not PROVEN complete stays open and is retried FOREVER with a bounded backoff;
        it is never abandoned and never recorded as closed."""
        attempts = (self.emergency or {}).get("attempts", 0) + 1
        self.emergency = {"setup_id": setup_id, "direction": str(direction).upper(), "reason": reason,
                          "attempts": attempts, "error": error[:200], "wait": self._gap_polls(attempts)}
        if attempts == 1 or attempts % LOG_EVERY_N_ATTEMPTS == 0:
            self._incident("EMERGENCY_CLOSE_INCOMPLETE" if error.startswith("status=") else "EMERGENCY_CLOSE_FAILED",
                           error, t_ms, attempts=attempts)
        return []

    def _emergency_flatten(self, setup_id: str, direction: str, reason: str, t_ms: int) -> list[Any]:
        """Close OUR exposure at market and PROVE it. Anything short of proven-flat stays an open
        emergency that is retried with bounded backoff until flat; the executor is halted throughout."""
        self._halt(reason[:200])
        self._cancel_own_resting(setup_id)
        hold = self._hold(direction)
        self._cancel_pending_closes(hold)
        try:
            result = self.client.close_futures_position_full(
                symbol=self.rules.symbol, direction=str(direction).upper(), margin_mode="isolated",
                reason="lsrl_unprotected_position", cleanup_tpsl=False)
        except Exception as exc:                            # noqa: BLE001
            return self._emergency_failed(setup_id, direction, reason, _reason(exc), t_ms)
        status = str((result or {}).get("status") or "").upper()
        try:
            remaining = self._read_positions()[hold]
        except Exception as exc:                            # noqa: BLE001
            return self._emergency_failed(setup_id, direction, reason, f"POSITION_READ:{_reason(exc)}", t_ms)
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
        return self._emergency_failed(setup_id, direction, reason, f"status={status} remaining={remaining}", t_ms)

    def _protection_failed(self, setup_id: str, direction: Any, reason: str, t_ms: int) -> list[Any]:
        """A position whose stop cannot be PROVEN is flattened: never knowingly unprotected."""
        d = str(getattr(direction, "value", direction)).upper()
        hold = self._hold(d)
        try:
            qty = self._read_positions()[hold]
        except Exception:                                   # noqa: BLE001
            qty = self.exposure.get("qty", 0.0)
        self._begin_unprotected(hold, qty, self.setups.get(setup_id), reason, t_ms)
        self._escalate(f"STOP_PLACEMENT_FAILED:{reason}", t_ms)
        events = self._emergency_flatten(setup_id, d, f"STOP_PLACEMENT_FAILED:{reason}", t_ms)
        if self.emergency is None:
            self._exposure_resolved("FLAT", t_ms)
        else:
            self.exposure["state"] = EXPOSURE_CRITICAL
            self.exposure["wait"] = self.emergency.get("wait", 0)
        return events

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
        if self._ledger(setup["hold"]) <= self.rules.qty_step / 2:
            return                                          # our books hold nothing (flattened): no take-profit can exist
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
            self._unknown_orders = True                     # a fill may be hiding behind this: see _owned_exposure
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
            if setup is not None and rec["kind"] == "ENTRY" and setup.get("fill_seen_ms") is None:
                setup["fill_seen_ms"] = self._clock_ms()
                exchange_ms = int(_f((row or {}).get("uTime") or (row or {}).get("cTime")))
                setup["fill_exchange_ms"] = exchange_ms or None
            if setup is not None and rec["kind"] == "ENTRY" and setup["engine_closed"]:
                setup["orphan_qty"] = setup.get("orphan_qty", 0.0) + delta
            if setup is not None and rec["kind"] == "ENTRY" and not setup["engine_closed"] and not self.emergency:
                self._protect_immediately(setup, t_ms)
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

    def _protect_immediately(self, setup: dict[str, Any], t_ms: int) -> None:
        """The moment an entry fill is SEEN, before any further read: place (and prove) the frozen stop for the whole
        ledgered quantity. This is the shortest path from 'fill visible' to 'stop confirmed'; every later read is a
        cross-check. A failure changes nothing here: the regular cycle in the same poll escalates it."""
        hold = setup["hold"]
        step = self.rules.qty_step
        qty = round(self._ledger(hold) / step) * step
        price = setup["stop_price"] if setup["stop_price"] is not None else setup.get("protect_price")
        if price is None or qty <= step / 2:
            return
        cur = self.stop
        if (cur and cur["setup_id"] == setup["setup_id"] and cur["qty"] + step / 2 >= qty
                and abs(cur["price"] - price) <= self.rules.tick_size / 2):
            return
        # The entry carried a preset stop: the exchange may ALREADY hold the stop. Prove it and adopt it; only if it is
        # not there (or does not cover the position) is a stop placed, which is the pre-existing behaviour.
        if self._adopt_exchange_stop(setup, price, qty, t_ms):
            self._record_protection_latency(setup, t_ms)
            self._persist()
            return
        try:
            self._install_stop(setup, t_ms, "ENTRY_FILL_IMMEDIATE", price=price, qty=qty)
        except Exception as exc:                            # noqa: BLE001
            self.log.warning("LSRL_LIVE_IMMEDIATE_PROTECTION_FAILED | %s | %s", setup["setup_id"], _reason(exc))
            self._immediate_failed_t, self._immediate_failed_why = t_ms, _reason(exc)   # spent: do not repeat them
            return
        self._record_protection_latency(setup, t_ms)
        self._persist()

    def _stop_contexts(self) -> list[dict[str, Any]]:
        """Every stop whose FILLS we must be able to read: our own installed stop, and - for setups that have entry fills
        but no stop of ours on record - the exchange-side preset stop that was created at the fill. Without the second
        kind, a stop that fires while no process is alive would look like an unexplained flat position."""
        contexts: list[dict[str, Any]] = []
        if self.stop:
            contexts.append(self.stop)
        for setup in self.setups.values():
            if setup["finalized"] or (self.stop and self.stop["setup_id"] == setup["setup_id"]):
                continue
            entries = self._orders_of(setup["setup_id"], "ENTRY")
            if sum(r["reported_qty"] for r in entries) <= EPS:
                continue
            price = setup["stop_price"] if setup["stop_price"] is not None else setup.get("protect_price")
            armed = int(setup.get("fill_exchange_ms") or setup.get("opened_ms") or 0)
            contexts.append({"setup_id": setup["setup_id"], "hold": setup["hold"], "price": price, "armed_ms": armed})
        return contexts

    def _stop_fill_events(self, t_ms: int) -> list[Any]:
        """Stop fills come from the exchange ORDER HISTORY (real price and fee), keyed by orderId with
        cumulative quantity: a position shrinking between two reads is never read as a stop."""
        contexts = self._stop_contexts()
        if not contexts:
            return []
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
            oid = str(row.get("orderId") or "")
            known = self.stop_fills.get(oid)
            stop = next((c for c in contexts if (known is None or c["setup_id"] == known.get("setup_id"))
                         and (not side or side == c["hold"])
                         and int(_f(row.get("cTime"))) + 5_000 >= c["armed_ms"]), None)
            if stop is None:
                continue
            if str(row.get("status") or row.get("state") or "").lower() not in ("filled", "partially_filled"):
                continue
            filled, avg, fee = _f(row.get("baseVolume")), _f(row.get("priceAvg")), abs(_f(row.get("fee")))
            seen = self.stop_fills.setdefault(oid, {"qty": 0.0, "notional": 0.0, "fee": 0.0, "hold": stop["hold"],
                                                    "setup_id": stop["setup_id"]})
            if filled > seen["qty"] + EPS:
                delta = filled - seen["qty"]
                fallback = stop["price"] if stop.get("price") else 0.0
                price = (filled * avg - seen["notional"]) / delta if delta > EPS and avg > 0 else fallback
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

    # ------------------------------------------------------------------ THE EXPOSURE STATE MACHINE
    def _claimed_by_us(self, hold: str) -> bool:
        try:
            owner = self.ownership.owner_of(self.rules.symbol, hold)
        except Exception:                                   # noqa: BLE001
            return False
        return bool(owner) and owner_family(owner) == owner_family(STRATEGY_ID)

    def _active_setup(self, hold: str) -> dict[str, Any] | None:
        live = [s for s in self.setups.values() if s["hold"] == hold and not s["finalized"]]
        return live[-1] if live else None

    def _owned_exposure(self, positions: dict[str, float], orders_unreadable: bool = False
                        ) -> dict[str, tuple[float, dict[str, Any] | None]]:
        """Exchange quantity that is LSRL's to protect. It is ours when (a) our ledger of exchange-confirmed fills says
        so, (b) our resting entry could not be read this cycle and exposure appeared on its side (a fill we cannot see
        yet), (c) the ownership registry says the side is ours but no setup explains it (executor state lost), or (d) an
        entry written ahead on that side is still unresolved (accepted, reply lost, lookup failing): its fill is ours.
        Anything else is foreign and is never touched: it halts, it is not protected or closed by us."""
        owned: dict[str, tuple[float, dict[str, Any] | None]] = {}
        unresolved = self._unresolved_entry_sides()          # an entry we wrote ahead but cannot see: its fill is OURS
        for hold, qty in positions.items():
            if qty <= self.rules.qty_step / 2:
                continue
            setup = self._active_setup(hold)
            resting = any(r["kind"] == "ENTRY" and not r["terminal"] and self.setups.get(r["setup_id"], {}).get("hold") == hold
                          for r in self.orders.values())
            if (self._ledger(hold) > EPS or (orders_unreadable and resting) or hold in unresolved
                    or (setup is None and self._claimed_by_us(hold))):
                owned[hold] = (qty, setup)
        return owned

    def engine_finished(self, setup_id: str, t_ms: int) -> None:
        """The engine ended this setup. Entries of it that still rest - tracked or adopted late - will never be managed by
        anyone: cancel them. A fill that nevertheless lands is exposure without an active setup and is flattened (verified)."""
        setup = self.setups.get(setup_id)
        if setup is None or setup["finalized"]:
            return
        setup["engine_closed"] = True
        setup["closed_ms"] = setup.get("closed_ms") or t_ms
        self._cancel_resting_entries(setup_id)
        self._persist()

    def _cancel_resting_entries(self, setup_id: str | None) -> None:
        """No additional ladder exposure while protection is unproven. Cancels are requests; late fills are still
        tracked and protected."""
        for rec in list(self.orders.values()):
            if rec["kind"] != "ENTRY" or rec["terminal"] or rec["cancel_requested"]:
                continue
            if setup_id is not None and rec["setup_id"] != setup_id:
                continue
            rec["cancel_requested"] = True
            try:
                self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=rec["client_oid"])
            except Exception as exc:                        # noqa: BLE001
                if not ("not exist" in str(exc).lower() or "43001" in str(exc)):
                    self._incident("ENTRY_CANCEL_FAILED", _reason(exc), tag=rec["tag"])
                    rec["cancel_requested"] = False         # try again on the next cycle

    def _begin_unprotected(self, hold: str, qty: float, setup: dict[str, Any] | None, why: str, t_ms: int) -> None:
        """Marks the state and makes it DURABLE before anything is attempted (a crash now restarts in this state).
        A fill found a moment ago is normally protected in this same cycle, so nothing else is disturbed yet."""
        ex = self.exposure
        if ex["state"] in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL):
            ex["qty"], ex["last_error"] = qty, why
            return
        ex.update({"state": EXPOSURE_UNPROTECTED, "hold": hold, "qty": qty, "since_ms": t_ms, "attempts": 0,
                   "wait": 0, "last_error": why, "setup_id": setup["setup_id"] if setup else None,
                   "escalated": False})
        self.log.warning("LSRL_LIVE_EXPOSURE_UNPROTECTED | %s qty=%s | %s", hold, qty, why)
        if self.stop and setup is not None and self.stop.get("setup_id") == setup["setup_id"]:
            self._incident("STOP_AUDIT_FAILED", why, t_ms, setup_id=setup["setup_id"])   # a stop we believed in is gone
        self._persist()

    def _escalate(self, reason: str, t_ms: int) -> None:
        """Protection could not be established at the first attempt: the incident is raised, NEW entries are blocked
        and no ADDITIONAL ladder exposure is left resting while the position is unproven."""
        ex = self.exposure
        if ex.get("escalated"):
            return
        ex["escalated"] = True
        if reason.startswith("STOP_PLACEMENT_FAILED"):
            self._incident("PROTECTION_FAILED", reason, t_ms, setup_id=ex.get("setup_id"))
        self._incident("UNPROTECTED_EXPOSURE", f"{ex.get('hold')} qty={ex.get('qty')}: {reason}", t_ms,
                       setup_id=ex.get("setup_id"))
        self._halt(reason[:200])
        self._cancel_resting_entries(ex.get("setup_id"))
        self._persist()

    def _exposure_resolved(self, how: str, t_ms: int) -> None:
        ex = self.exposure
        was = ex["state"]
        ex["state"] = EXPOSURE_PROTECTED if how == "PROTECTED" else EXPOSURE_NONE
        if was in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL):
            if ex.get("escalated"):
                ex["recoveries"] = ex.get("recoveries", 0) + 1
                self._incident("EXPOSURE_RECOVERED", f"{how} after {ex.get('attempts', 0)} attempt(s)", t_ms)
            ex.update({"attempts": 0, "wait": 0, "last_error": "", "since_ms": None, "escalated": False})
        elif how == "FLAT":
            ex.update({"hold": None, "qty": 0.0, "setup_id": None})
        ex["escalated"] = False if ex["state"] in (EXPOSURE_NONE, EXPOSURE_PROTECTED) else ex.get("escalated", False)
        self._persist()

    def _record_protection_latency(self, setup: dict[str, Any], t_ms: int) -> None:
        if setup.get("protected_ms") is not None or setup.get("fill_seen_ms") is None:
            return
        now = self._clock_ms()
        setup["protected_ms"] = now
        entry = {"setup_id": setup["setup_id"], "discovery_to_verified_ms": now - setup["fill_seen_ms"]}
        if setup.get("fill_exchange_ms"):
            entry["exchange_fill_to_discovery_ms"] = setup["fill_seen_ms"] - setup["fill_exchange_ms"]
            entry["exchange_fill_to_verified_ms"] = now - setup["fill_exchange_ms"]
        self.latencies.append(entry)
        self.log.warning("LSRL_LIVE_PROTECTION_LATENCY | %s", entry)

    def _ensure_protected(self, hold: str, qty: float, setup: dict[str, Any] | None, t_ms: int) -> list[Any]:
        """One reconciliation step for exposure we own: verified stop, or repair, or verified flat."""
        events: list[Any] = []
        ex = self.exposure
        price = None
        if setup is not None:
            price = setup["stop_price"] if setup["stop_price"] is not None else setup.get("protect_price")
        why = "NO_STOP_PRICE_KNOWN"
        # With preset stops the exchange may hold a stop we never placed, so there is no "known unprotected" shortcut:
        # the read-back below adopts it or reports why it is not enough.
        if price is not None:
            self._verify_first = False
            ok, why = self._verify_stop(hold, price, qty)
            if ok:
                if self.stop and self.stop.get("setup_id") == setup["setup_id"]:
                    self.stop["verified_ms"] = t_ms
                else:
                    self._adopt_exchange_stop(setup, price, qty, t_ms)
                self._record_protection_latency(setup, t_ms)
                if ex["state"] != EXPOSURE_PROTECTED or ex["hold"] != hold:
                    ex.update({"hold": hold, "qty": qty, "setup_id": setup["setup_id"]})
                    self._exposure_resolved("PROTECTED", t_ms)
                events += self._check_engine_ack(hold, setup, t_ms)
                return events
        self._begin_unprotected(hold, qty, setup, why, t_ms)
        if self.emergency:
            self._escalate(f"UNPROTECTED_EXPOSURE:{why}", t_ms)
            ex["state"] = EXPOSURE_CRITICAL                 # a flatten is already in flight; it retries on its own clock
            return events
        if ex.get("wait", 0) > 0:
            ex["wait"] -= 1
            return events                                   # bounded backoff between attempts (counted in polls)
        ex["attempts"] += 1
        if self._immediate_failed_t == t_ms:
            why = self._immediate_failed_why                # what the immediate attempt actually saw
        ex["last_error"] = why
        if price is not None and self._immediate_failed_t != t_ms:
            try:
                self._install_stop(setup, t_ms, "EXPOSURE_RECOVERY", price=price, qty=qty)
                self._record_protection_latency(setup, t_ms)
                self._exposure_resolved("PROTECTED", t_ms)
                return events
            except Exception as exc:                        # noqa: BLE001
                why = _reason(exc)
                ex["last_error"] = why
        reason = f"STOP_PLACEMENT_FAILED:{why}" if price is not None else f"UNPROTECTED_EXPOSURE:{why}"
        self._escalate(reason, t_ms)
        sid = setup["setup_id"] if setup else (ex.get("setup_id") or "")
        events += self._emergency_flatten(sid, "LONG" if hold == "long" else "SHORT", reason[:200], t_ms)
        if self.emergency is None:
            self._exposure_resolved("FLAT", t_ms)
        else:
            ex["state"] = EXPOSURE_CRITICAL
            ex["wait"] = self._gap_polls(ex["attempts"])
        return events

    def _check_engine_ack(self, hold: str, setup: dict[str, Any] | None, t_ms: int) -> list[Any]:
        """The engine manages targets and the stop lifecycle. A fill it never acknowledged (its SetStop never covers
        the ledger) means it does not know this position: hold the protective stop, then flatten after a grace."""
        if setup is None or setup["engine_closed"] or self.emergency:
            return []
        unacked = self._ledger(hold) - setup.get("ack_qty", 0.0)
        if unacked <= self.rules.qty_step / 2:
            setup["unacked_since_ms"] = None
            return []
        if setup.get("unacked_since_ms") is None:
            setup["unacked_since_ms"] = t_ms
            return []
        if t_ms - setup["unacked_since_ms"] < ENGINE_ACK_GRACE_MS:
            return []
        self._incident("ENGINE_DID_NOT_ACKNOWLEDGE_FILL", f"{hold}: filled={self._ledger(hold)} "
                       f"acknowledged={setup.get('ack_qty', 0.0)} for {t_ms - setup['unacked_since_ms']} ms", t_ms,
                       setup_id=setup["setup_id"])
        return self._emergency_flatten(setup["setup_id"], "LONG" if hold == "long" else "SHORT",
                                       "ENGINE_DID_NOT_ACKNOWLEDGE_FILL", t_ms)

    def _exposure_cycle(self, positions: dict[str, float], t_ms: int, orders_unreadable: bool = False) -> list[Any]:
        events: list[Any] = []
        owned = self._owned_exposure(positions, orders_unreadable)
        if not owned:
            if self.exposure["state"] != EXPOSURE_NONE:
                self._exposure_resolved("FLAT", t_ms)
            return events
        for hold, (qty, setup) in owned.items():
            events += self._ensure_protected(hold, qty, setup, t_ms)
        return events

    def _audit_protection(self, positions: dict[str, float], t_ms: int) -> list[Any]:
        """Every poll: exposure we own has a PROVEN stop, or it is repaired, or it is flattened (see above)."""
        return self._exposure_cycle(positions, t_ms)

    # ------------------------------------------------------------------ restart recovery
    def _resolve_expected_entries(self, t_ms: int, *, startup: bool = False) -> bool:
        """Entries recorded WRITE-AHEAD but never tracked (the process died, or the reply was lost, after the exchange may have
        accepted them) are looked up by their deterministic clientOid and adopted. This runs at start AND on every poll: an
        accepted-but-unknown entry that fills in a RUNNING process must not sit as unexplained exposure. Returns True when
        nothing is left unresolved. An unreadable answer is never 'absent': it stays unresolved and blocks new exposure."""
        resolved = True
        for setup in self.setups.values():
            for oid, exp in list(setup.get("expected_entries", {}).items()):
                if oid in self.orders:
                    del setup["expected_entries"][oid]
                    continue
                try:
                    found = self.client.find_order_by_client_oid(self.rules.symbol, oid)
                    status = str(found.get("status"))
                except Exception as exc:                    # noqa: BLE001
                    status, found = "UNKNOWN", {}
                    self.log.warning("LSRL_LIVE_ENTRY_LOOKUP_FAILED | %s | %s", oid, _reason(exc))
                if status == "UNKNOWN":
                    self._halt(f"{'STARTUP_' if startup else ''}ORDER_STATE_UNKNOWN:{oid}")   # no new exposure until resolved
                    resolved = False
                    continue
                if status == "ABSENT":
                    exp["absent_polls"] = exp.get("absent_polls", 0) + 1
                    if startup or exp["absent_polls"] >= EXPECTED_ABSENT_GRACE_POLLS:
                        del setup["expected_entries"][oid]  # it was never placed
                    else:
                        resolved = False                    # visibility can lag an accepted order: do not drop it yet
                    continue
                row = found.get("order") or {}
                self.orders[oid] = self._order_rec(exp["tag"], "ENTRY", setup["setup_id"], exp["rung"], oid,
                                                   str(row.get("orderId") or ""), exp["price"], exp["qty"],
                                                   exp["direction"], exp["t_ms"], preset_stop=setup.get("protect_price"))
                del setup["expected_entries"][oid]
                self._incident("ENTRY_ORDER_ADOPTED_AFTER_RESTART" if startup else "ENTRY_ORDER_ADOPTED", f"{oid} "
                               f"state={status}", t_ms, setup_id=setup["setup_id"])
                if setup["engine_closed"] and str(row.get("state") or row.get("status") or "").lower() in (
                        "live", "new", "partially_filled"):
                    self.orders[oid]["cancel_requested"] = True    # the engine has finished with this setup: do not leave it resting
                    try:
                        self.client.cancel_futures_order(symbol=self.rules.symbol, client_oid=oid)
                    except Exception as exc:                # noqa: BLE001
                        if not ("not exist" in str(exc).lower() or "43001" in str(exc)):
                            self.orders[oid]["cancel_requested"] = False   # try again through the normal path
        return resolved

    def _unresolved_entry_sides(self) -> set[str]:
        return {s["hold"] for s in self.setups.values() if s.get("expected_entries")}

    def _startup_reconcile(self, t_ms: int) -> list[Any]:
        """Once per process, before anything else: write-ahead entries are resolved (see above); an unreadable answer keeps
        new exposure blocked and is retried until it is resolved."""
        if self._recovered:
            return []
        if not self._resolve_expected_entries(t_ms, startup=True):
            return []
        if not self._startup_audit(t_ms):
            return []                                       # the exchange could not be read in full: nothing is assumed
        self._recovered = True
        self._persist()
        return []

    def _side_is_ours(self, hold: str) -> bool:
        """The registry says the side is LSRL's, or LSRL has traded it before (a setup exists in our own records)."""
        return self._claimed_by_us(hold) or any(s["hold"] == hold for s in self.setups.values())

    def _startup_audit(self, t_ms: int) -> bool:
        """THE EXCHANGE IS AUTHORITATIVE. Once per process, before anything is managed, read everything the exchange holds on
        the symbol - positions, resting orders, plan orders - and classify it against local state, never the reverse:
          * a resting order we track                      -> counted;
          * a reduce-only take-profit named for us (`lsrl...`) that we no longer track -> CANCELLED (it occupies the position
            in hedge mode and nobody manages it);
          * an untracked resting ENTRY on a side we own (state lost) -> CANCELLED: it could fill into exposure we cannot manage;
          * any other untracked order                     -> LEFT ALONE and reported (it may belong to someone else); it blocks
            new entries through the entry-cycle gate;
          * a stop/take-profit PLAN order on a side that is flat and has no live setup -> CANCELLED if the side is ours
            (a leftover stop would later 'protect' a NEW position at a stale price), otherwise LEFT ALONE and reported.
        Position protection itself is the poll's job (its stop read-back adopts or repairs). Returns False, and keeps new
        exposure blocked, when any of the three reads fails: an unreadable answer is never an empty account."""
        report: dict[str, Any] = {"t_ms": t_ms, "tracked_live": [], "orphan_orders_cancelled": [], "unclassified_orders": [],
                                  "orphan_plans_cancelled": [], "foreign_plans": [], "stops": [], "cancel_failures": []}
        try:
            positions = self._read_positions()
            pending = _rows(self.client.get_pending_orders(symbol=self.rules.symbol))
            plans = self._plan_rows()
        except Exception as exc:                            # noqa: BLE001
            self._halt(f"STARTUP_EXCHANGE_UNREADABLE:{_reason(exc)}")
            report["unreadable"] = _reason(exc)
            self.startup_report = report
            return False
        report["positions"] = dict(positions)
        for row in pending:
            oid = str(row.get("clientOid") or "")
            hold = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if oid in self.orders:
                report["tracked_live"].append(oid)
                continue
            opens = str(row.get("tradeSide") or "").lower() == "open"
            if oid.startswith("lsrl") or (opens and hold and self._side_is_ours(hold)):
                kind = "orphan_orders_cancelled"
                try:
                    self.client.cancel_futures_order(symbol=self.rules.symbol, order_id=str(row.get("orderId") or "") or None,
                                                     client_oid=oid or None)
                    report[kind].append(oid or str(row.get("orderId")))
                    self._incident("ORPHAN_ORDER_CANCELLED", f"{oid} tradeSide={row.get('tradeSide')} side={hold}", t_ms)
                except Exception as exc:                    # noqa: BLE001
                    text = str(exc).lower()
                    if not ("not exist" in text or "43001" in text):
                        report["cancel_failures"].append({"order": oid, "error": _reason(exc)})
                        self._halt(f"STARTUP_ORPHAN_CANCEL_FAILED:{oid}")
            else:
                report["unclassified_orders"].append({"clientOid": oid, "side": hold, "tradeSide": row.get("tradeSide")})
                self._incident("UNCLASSIFIED_ORDER_LEFT_ALONE", f"{oid} side={hold}", t_ms)
        cancelled_sides: set[str] = set()
        for plan in plans:
            ptype = str(plan.get("planType") or plan.get("orderType") or "").lower()
            hold = str(plan.get("posSide") or plan.get("holdSide") or "").lower()
            report["stops"].append({"type": ptype, "side": hold, "trigger": plan.get("triggerPrice"), "size": plan.get("size")})
            flat = positions.get(hold, 0.0) <= self.rules.qty_step / 2 and self._active_setup(hold) is None
            if not (hold and flat):
                continue
            if self._side_is_ours(hold):
                if hold not in cancelled_sides:
                    try:
                        self.client.cancel_all_futures_tpsl_orders(symbol=self.rules.symbol, hold_side=hold)
                        cancelled_sides.add(hold)
                        self._incident("ORPHAN_PLAN_ORDERS_CANCELLED", f"side={hold} (flat, no live setup)", t_ms)
                    except Exception as exc:                # noqa: BLE001
                        report["cancel_failures"].append({"plan_side": hold, "error": _reason(exc)})
                        self._halt(f"STARTUP_ORPHAN_PLAN_CANCEL_FAILED:{hold}")
                report["orphan_plans_cancelled"].append({"type": ptype, "side": hold, "trigger": plan.get("triggerPrice")})
            else:
                report["foreign_plans"].append({"type": ptype, "side": hold, "trigger": plan.get("triggerPrice")})
                self._incident("FOREIGN_PLAN_ORDER_LEFT_ALONE", f"{ptype} side={hold}", t_ms)
        self.startup_report = report
        return True

    def _fast_watch(self, t_ms: int) -> list[Any]:
        """The runtime polls every 3 s. While an entry rests (or exposure is unresolved) the executor looks for the
        fill itself about once a second, so the time a fill sits unprotected is bounded by this, not by the poll."""
        if not self._fast_watch_enabled or self._in_poll or not self._recovered:
            return []
        resting = (any(r["kind"] == "ENTRY" and not r["terminal"] for r in self.orders.values())
                   or bool(self._unresolved_entry_sides()))
        if not (resting or self.emergency or self.exposure["state"] in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL)):
            return []
        if t_ms - self._last_fast_ms < FAST_WATCH_MS:
            return []
        self._last_fast_ms = t_ms
        try:
            return self.poll(t_ms)
        except Exception as exc:                            # noqa: BLE001 - a failed look must never break the trading loop
            self._read_failure("FAST_WATCH_FAILED", exc, t_ms)
            return []

    # ------------------------------------------------------------------ poll
    def poll(self, t_ms: int) -> list[Any]:
        """Read the exchange and translate reality into engine events. Never adds exposure."""
        if self._in_poll:
            return []
        self._in_poll = True
        try:
            return self._poll(t_ms)
        finally:
            self._in_poll = False

    def _read_failure(self, kind: str, exc: Exception, t_ms: int) -> None:
        count = self._read_failures[kind] = self._read_failures.get(kind, 0) + 1
        if count == 1 or count % LOG_EVERY_N_ATTEMPTS == 0:
            self._incident(kind, _reason(exc), t_ms, occurrences=count)

    def _poll(self, t_ms: int) -> list[Any]:
        events: list[Any] = []
        events += self._startup_reconcile(t_ms)
        if self._recovered:
            self._resolve_expected_entries(t_ms)             # a write-ahead entry from THIS process, accepted but never tracked
        active = (any(not r["terminal"] for r in self.orders.values()) or self.stop or self.emergency
                  or bool(self._unresolved_entry_sides())
                  or self.pending_central or any(not s["finalized"] for s in self.setups.values())
                  or self.exposure["state"] != EXPOSURE_NONE or not self._recovered or not self._polled_once)
        self._polled_once = True                            # a fresh process ALWAYS reads the exchange once: state may be lost
        if not active:
            return events
        if self.emergency:
            events += self._retry_emergency(t_ms)
        orders_unreadable = False
        self._unknown_orders = False
        try:
            events += self._reconcile_orders(t_ms)
        except Exception as exc:                            # noqa: BLE001 - an unreadable order must not stop protection
            orders_unreadable = True
            self._read_failure("ORDER_RECONCILE_FAILED", exc, t_ms)
        orders_unreadable = orders_unreadable or self._unknown_orders
        self._persist()                                     # the fills just seen are durable BEFORE we act on them
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
        if positions is not None:
            events += self._exposure_cycle(positions, t_ms, orders_unreadable)
            if not self.emergency:
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
        if em.get("wait", 0) > 0:
            em["wait"] -= 1
            return []
        return self._emergency_flatten(em["setup_id"], em["direction"], em["reason"], t_ms)

    # ------------------------------------------------------------------ lifecycle / ownership / central record
    def _advance_lifecycles(self, t_ms: int, positions: dict[str, float] | None) -> list[Any]:
        for sid, setup in self.setups.items():
            if setup["finalized"] or self.emergency:
                continue
            if setup.get("expected_entries"):
                continue                                    # an entry written ahead but not yet resolved may still fill: not finished
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
        """Explicit owner action only; never called by the runtime. Refused while exposure is unresolved: the
        recovery loop, not a person clicking resume, ends an emergency."""
        if self.emergency or self.exposure["state"] in (EXPOSURE_UNPROTECTED, EXPOSURE_CRITICAL):
            raise LiveExecutorError(f"cannot resume while exposure is unresolved: state={self.exposure['state']} "
                                    f"emergency={bool(self.emergency)}")
        self._incident("RESUME", f"halt cleared (was {self.halt_reason})")
        self.halt_reason = None
        self._persist()
