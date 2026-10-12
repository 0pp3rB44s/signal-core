"""Controlled one-trade smoke lifecycle: ENTRY -> FILL -> SL/TP -> EXIT -> JOURNAL.

This is NOT a strategy and NOT the production runner. It exercises the real
Bitget client methods through a small `ExchangePort`, one trade at a time, with
every step confirmed against exchange truth:

  PRECHECK -> SIZING -> ENTRY_PERSISTED -> ENTRY_SUBMITTED -> FILL_CONFIRMED
           -> PROTECTION_CONFIRMED -> EXIT_SUBMITTED -> FLAT_CONFIRMED -> DONE

Invariants:
* An API acknowledgement is never a fill; a position exists only after the
  position endpoint shows it. A position is never "closed" until it reads flat.
* The entry goes through the production `EntryOrderSubmitter.submit_entry` (the
  project's single protected entry path, tests/test_entry_path_audit.py): the
  order intent (clientOid) is persisted in `state/order_intents.json` BEFORE the
  POST, so the bot's own startup recovery also reconciles a crashed smoke run.
  An ambiguous submit is never re-sent - it is reconciled or the run BLOCKS.
* Stop-loss not confirmed -> fail-safe full close; close not confirmed ->
  emergency stop engaged and OPERATOR ATTENTION escalated.
* Exchange state unknown -> no order.
* Account settings (leverage / margin mode / position mode) are READ and must
  already match the spec; this module never changes them.
* Real execution needs the full authorisation tuple in `authorise()`.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from execution.emergency_stop import EmergencyStop

HARD_NOTIONAL_CAP_USDT = 15.0
AUTH_ENV = "LIVE_SMOKE_AUTH_SHA256"
AUTH_FILE = "state/LIVE_SMOKE_AUTHORISATION"
TERMINAL = {"DONE", "ABORTED", "FAILSAFE_CLOSED", "BLOCKED_UNKNOWN", "OPERATOR_ATTENTION"}


class SmokeAbort(Exception):
    """Abort before any order exists."""


# ------------------------------------------------------------------ spec
@dataclass(frozen=True)
class SmokeSpec:
    symbol: str
    direction: str                 # LONG | SHORT
    notional_usdt: float
    stop_pct: float                # distance below/above entry
    take_profit_pct: float
    required_leverage: int         # must already be configured on the exchange (isolated)
    margin_mode: str = "isolated"
    pos_mode: str = "hedge_mode"
    max_notional_usdt: float = HARD_NOTIONAL_CAP_USDT
    fill_timeout_s: float = 20.0
    hold_seconds: float = 0.0      # 0 = exit right after protection is confirmed
    taker_fee_rate: float = 0.0006

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def confirm_phrase(self) -> str:
        return f"PLACE ONE REAL ORDER {self.symbol} {self.direction} {self.notional_usdt:g} USDT"


# ------------------------------------------------------------------ port
class ExchangePort(Protocol):
    def account(self, symbol: str) -> dict[str, Any]: ...        # equity, available, pos_mode, margin_mode, long_lever, short_lever
    def positions(self) -> list[dict[str, Any]]: ...             # raises on unknown
    def open_orders(self) -> list[dict[str, Any]]: ...           # raises on unknown
    def plan_orders(self) -> list[dict[str, Any]]: ...           # raises on unknown
    def last_price(self, symbol: str) -> float: ...
    def contract(self, symbol: str) -> dict[str, Any]: ...       # status, min_trade_usdt, min_trade_num
    def normalize_size(self, symbol: str, size: float, ref_price: float) -> tuple[float, str | None]: ...
    def make_plan(self, symbol: str, direction: str, tag: str) -> Any: ...                        # TradePlan-compatible identity object
    def entry_client_oid(self, plan: Any) -> str: ...
    def submit_entry(self, plan: Any, size: float, notional: float, ref_price: float) -> dict[str, Any]: ...  # via EntryOrderSubmitter
    def lookup_order(self, symbol: str, client_oid: str) -> dict[str, Any]: ...   # {"status": FOUND|ABSENT|UNKNOWN, "order": ...}
    def place_protection(self, symbol: str, direction: str, size: float, stop: float, tps: list[float]) -> dict[str, Any]: ...
    def close_full(self, symbol: str, direction: str, size: float, reason: str) -> dict[str, Any]: ...


class BitgetPort:
    """Adapter over the production `BitgetRestClient`. GET + the order/close calls the bot already uses."""

    def __init__(self, client: Any, product_type: str = "USDT-FUTURES", *, bot_identity: str = "bgai",
                 intent_path: str = "state/order_intents.json") -> None:
        self.c, self.pt = client, product_type
        self.bot_identity, self.intent_path = bot_identity, intent_path
        self._sub: Any = None

    def _submitter(self) -> Any:
        if self._sub is None:
            import logging

            from execution.entry_submitter import EntryOrderSubmitter
            from execution.order_intent_store import OrderIntentStore

            self._sub = EntryOrderSubmitter(client=self.c, intent_store=OrderIntentStore(self.intent_path),
                                            session_id=f"smoke-{uuid.uuid4().hex[:10]}", execution_mode="LIVE",
                                            bot_identity=self.bot_identity, log=logging.getLogger("smoke_lifecycle"))
        return self._sub

    def make_plan(self, symbol, direction, tag):
        from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
        from clients.schemas import TradePlan

        candle_ms = int(time.time() * 1000)
        cid = deterministic_candidate_id("smoke_lifecycle", symbol, direction, candle_ms)
        return TradePlan(candidate_id=cid, candidate_candle_open_timestamp_ms=candle_ms, plan_id=deterministic_plan_id(cid),
                         symbol=symbol, strategy="smoke_lifecycle", direction=direction, verdict="EXECUTABLE", score=0.0,
                         entry_prices=[0.0], stop_loss=0.0, take_profits=[], risk_reward_ratio=0.0, account_risk_pct=0.0,
                         leverage=0.0, position_notional_usdt=0.0, notes=[f"smoke:{tag}"], reasons=[], geometry_entry=0.0)

    def entry_client_oid(self, plan):
        return self._submitter().client_oid_for(plan)

    def submit_entry(self, plan, size, notional, ref_price):
        def _place(client_oid):  # fed by the submitter, which persisted the intent first
            return self.c.place_futures_market_order(symbol=plan.symbol, direction=plan.direction, size=size, client_oid=client_oid,
                                                     margin_mode="isolated", reference_price=ref_price)

        r = self._submitter().submit_entry(plan=plan, size=size, side="buy" if plan.direction == "LONG" else "sell",
                                           order_type="market", leg="market", place=_place, notional_usdt=notional)
        return {"status": r.status, "client_oid": r.client_oid, "order_id": r.order_id, "classification": r.classification,
                "message": r.message}

    def account(self, symbol):
        cfg = (self.c._request("GET", "/api/v2/mix/account/account",
                               params={"symbol": symbol, "productType": self.pt, "marginCoin": "USDT"}, private=True).get("data") or {})
        return {"equity": _f(cfg.get("accountEquity") or cfg.get("usdtEquity")), "available": _f(cfg.get("available")),
                "pos_mode": cfg.get("posMode"), "margin_mode": cfg.get("marginMode"),
                "long_lever": _f(cfg.get("isolatedLongLever")), "short_lever": _f(cfg.get("isolatedShortLever"))}

    def positions(self):
        return list(self.c.get_all_positions().get("data") or [])

    def open_orders(self):
        d = self.c.get_open_orders().get("data") or {}
        return list((d.get("entrustedList") if isinstance(d, dict) else d) or [])

    def plan_orders(self):
        return list(self.c._extract_plan_list(self.c.get_tpsl_orders()) or [])

    def last_price(self, symbol):
        d = (self.c.get_symbol_price(symbol).get("data") or [{}])
        d = d[0] if isinstance(d, list) else d
        return _f(d.get("price") or d.get("markPrice"))

    def contract(self, symbol):
        rows = self.c._request("GET", "/api/v2/mix/market/contracts", params={"productType": self.pt, "symbol": symbol}).get("data") or [{}]
        r = rows[0]
        return {"status": r.get("symbolStatus"), "min_trade_usdt": _f(r.get("minTradeUSDT")), "min_trade_num": _f(r.get("minTradeNum")),
                "taker_fee": _f(r.get("takerFeeRate")), "maker_fee": _f(r.get("makerFeeRate"))}

    def normalize_size(self, symbol, size, ref_price):
        n, reason = self.c.validate_entry_size(symbol, size, reference_price=ref_price)
        return float(n), reason

    def lookup_order(self, symbol, client_oid):
        return self.c.find_order_by_client_oid(symbol, client_oid)

    def place_protection(self, symbol, direction, size, stop, tps):
        hold = "long" if direction == "LONG" else "short"
        return self.c.place_futures_protection_orders(symbol=symbol, direction=direction, hold_side=hold, size=size,
                                                      stop_loss=stop, take_profits=tps, margin_mode="isolated")

    def close_full(self, symbol, direction, size, reason):
        return self.c.close_futures_position_full(symbol=symbol, direction=direction, size=size, reason=reason)


def _f(x: Any) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------- journal
class Journal:
    """Append-only JSONL with a hash chain (tamper-evident), flushed per event."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq, self.prev = 0, "0" * 64
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    self.seq, self.prev = r["seq"], r["hash"]
                except Exception:
                    continue

    def write(self, run_id: str, phase: str, **data: Any) -> dict[str, Any]:
        self.seq += 1
        rec = {"seq": self.seq, "t_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "run_id": run_id,
               "phase": phase, "data": data, "prev": self.prev}
        rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.prev = rec["hash"]
        return rec

    @staticmethod
    def verify(path: Path) -> bool:
        prev = "0" * 64
        for line in Path(path).read_text().splitlines():
            r = json.loads(line)
            h = r.pop("hash")
            if r["prev"] != prev or hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest() != h:
                return False
            prev = h
        return True


# ------------------------------------------------------------ authorise
def authorise(spec: SmokeSpec, *, typed_phrase: str, env: dict[str, str] | None = None, auth_file: str | os.PathLike = AUTH_FILE) -> list[str]:
    """Return a list of failure reasons; empty list == authorised for a REAL order."""
    env = os.environ if env is None else env
    why: list[str] = []
    h = spec.sha256()
    if env.get(AUTH_ENV) != h:
        why.append(f"{AUTH_ENV} does not equal the spec sha256")
    p = Path(auth_file)
    if not p.exists():
        why.append(f"{p} missing (owner-created token)")
    else:
        txt = p.read_text()
        if f"spec_sha256={h}" not in txt or "authorised_by=" not in txt:
            why.append(f"{p} does not contain spec_sha256={h[:12]}… and authorised_by=")
    if typed_phrase != spec.confirm_phrase():
        why.append("typed confirmation phrase mismatch")
    if spec.notional_usdt > min(spec.max_notional_usdt, HARD_NOTIONAL_CAP_USDT):
        why.append(f"notional {spec.notional_usdt} exceeds cap {min(spec.max_notional_usdt, HARD_NOTIONAL_CAP_USDT)}")
    if spec.direction not in {"LONG", "SHORT"}:
        why.append("direction must be LONG or SHORT")
    return why


# ------------------------------------------------------------------ run
class SmokeRun:
    def __init__(self, port: ExchangePort, spec: SmokeSpec, *, state_path: str | os.PathLike, journal_path: str | os.PathLike,
                 emergency: EmergencyStop | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, poll_s: float = 1.0) -> None:
        self.port, self.spec = port, spec
        self.state_path = Path(state_path)
        self.emergency = emergency or EmergencyStop()
        self.sleep, self.clock, self.poll_s = sleep, clock, poll_s
        self.j = Journal(Path(journal_path), clock)
        self.run_id = uuid.uuid4().hex[:12]
        self.state: dict[str, Any] = {}

    # ---- state
    def _save(self, **kw: Any) -> None:
        self.state.update(kw, updated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True, default=str))
        os.replace(tmp, self.state_path)

    def _phase(self, phase: str, **data: Any) -> None:
        self._save(phase=phase)
        self.j.write(self.run_id, phase, **data)

    # ---- helpers
    def _position(self, symbol: str, direction: str) -> dict[str, Any] | None:
        hold = "long" if direction == "LONG" else "short"
        for p in self.port.positions():  # raises when unknown
            if str(p.get("symbol", "")).upper() == symbol and str(p.get("holdSide", "")).lower() == hold:
                if (_f(p.get("total")) or 0) > 0:
                    return p
        return None

    def _flat(self, symbol: str, direction: str) -> bool:
        return self._position(symbol, direction) is None

    def _wait(self, predicate: Callable[[], Any], timeout_s: float) -> Any:
        t0 = self.clock()
        while True:
            v = predicate()
            if v:
                return v
            if self.clock() - t0 >= timeout_s:
                return None
            self.sleep(self.poll_s)

    # ---- precheck (read-only)
    def precheck(self) -> dict[str, Any]:
        s = self.spec
        if self.emergency.is_active():
            raise SmokeAbort(self.emergency.reason())
        try:
            acc = self.port.account(s.symbol)
            positions, orders, plans = self.port.positions(), self.port.open_orders(), self.port.plan_orders()
            price = self.port.last_price(s.symbol)
            ct = self.port.contract(s.symbol)
        except Exception as exc:
            raise SmokeAbort(f"exchange state UNKNOWN ({type(exc).__name__}: {str(exc)[:120]}); no order") from exc
        if positions or orders or plans:
            raise SmokeAbort(f"account not flat: positions={len(positions)} open_orders={len(orders)} plan_orders={len(plans)}")
        if str(acc.get("pos_mode")) != s.pos_mode:
            raise SmokeAbort(f"posMode {acc.get('pos_mode')!r} != spec {s.pos_mode!r} (never changed by this tool)")
        if str(acc.get("margin_mode")) != s.margin_mode:
            raise SmokeAbort(f"marginMode {acc.get('margin_mode')!r} != spec {s.margin_mode!r} (never changed by this tool)")
        lever = acc.get("long_lever" if s.direction == "LONG" else "short_lever")
        if lever is None or int(lever) != int(s.required_leverage):
            raise SmokeAbort(f"configured {s.direction} leverage {lever} != required {s.required_leverage} (never changed by this tool)")
        if str(ct.get("status", "")).lower() != "normal":
            raise SmokeAbort(f"contract status {ct.get('status')!r} is not normal")
        if not price or price <= 0:
            raise SmokeAbort("no valid last price")
        margin = s.notional_usdt / s.required_leverage
        fees = s.notional_usdt * s.taker_fee_rate * 2
        need = margin * 1.5 + fees
        avail = acc.get("available")
        if avail is None or avail < need:
            raise SmokeAbort(f"insufficient available margin: have {avail} USDT, need >= {need:.4f} (margin {margin:.4f} x1.5 + fees {fees:.4f})")
        return {"account": acc, "price": price, "contract": ct, "margin_usdt": margin, "expected_fees_usdt": fees}

    # ---- main
    def run(self, *, execute: bool, authorised: bool = False) -> dict[str, Any]:
        s = self.spec
        self._save(run_id=self.run_id, spec=asdict(s), spec_sha256=s.sha256(), execute=execute, phase="START")
        try:
            pre = self.precheck()
            self._phase("PRECHECK", **{k: v for k, v in pre.items() if k != "contract"}, contract=pre["contract"])
            size, reason = self.port.normalize_size(s.symbol, s.notional_usdt / pre["price"], pre["price"])
            if reason or size <= 0:
                raise SmokeAbort(f"size not tradable: {reason or 'zero size'}")
            if size * pre["price"] > min(s.max_notional_usdt, HARD_NOTIONAL_CAP_USDT) * 1.02:
                raise SmokeAbort("normalised notional exceeds cap")
            sign = 1 if s.direction == "LONG" else -1
            stop = pre["price"] * (1 - sign * s.stop_pct / 100)
            tp = pre["price"] * (1 + sign * s.take_profit_pct / 100)
            self._phase("SIZING", size=size, ref_price=pre["price"], stop=stop, take_profit=tp, notional=size * pre["price"])
            if not execute:
                self._phase("DRY_RUN_COMPLETE", note="no order was sent")
                return self._result("DRY_RUN_COMPLETE")
            if not authorised:
                raise SmokeAbort("execute requested without authorisation")
        except SmokeAbort as exc:
            self._phase("ABORTED", reason=str(exc))
            return self._result("ABORTED", reason=str(exc))

        # ---- ENTRY: through the production EntryOrderSubmitter (intent persisted first, one POST, classified outcome)
        plan = self.port.make_plan(s.symbol, s.direction, self.run_id)
        client_oid = self.port.entry_client_oid(plan)
        self._save(client_oid=client_oid, size=size, stop=stop, take_profit=tp)
        self._phase("ENTRY_PERSISTED", client_oid=client_oid, size=size, path="EntryOrderSubmitter.submit_entry")
        try:
            res = self.port.submit_entry(plan, size, size * pre["price"], pre["price"])
        except Exception as exc:  # ambiguous: NEVER re-send; reconcile below
            self._phase("ENTRY_SUBMIT_EXCEPTION", client_oid=client_oid, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            return self._after_entry()
        status = str(res.get("status", "")).upper()
        self._phase("ENTRY_SUBMITTED", client_oid=client_oid, status=status, classification=res.get("classification"),
                    order_id=res.get("order_id"), note="ACK_IS_NOT_A_FILL")
        if status in {"REJECTED", "NOT_SENT"} or (status == "ABANDONED" and str(res.get("classification", "")).upper() == "ABSENT"):
            reason = f"entry not placed: {status}/{res.get('classification')}: {str(res.get('message'))[:120]}"
            self._phase("ABORTED", reason=reason)
            return self._result("ABORTED", reason=reason)
        if status == "BLOCKED_UNKNOWN":
            return self._block_unknown(f"entry outcome UNKNOWN per submitter ({res.get('classification')})")
        return self._after_entry()

    def _after_entry(self) -> dict[str, Any]:
        """Common path for first run and restart: reconcile, protect, exit. Never opens a second position."""
        s, st = self.spec, self.state
        client_oid = st.get("client_oid")
        try:
            pos = self._wait(lambda: self._position(s.symbol, s.direction), s.fill_timeout_s)
        except Exception as exc:
            return self._block_unknown(f"position read failed after entry: {type(exc).__name__}")
        if pos is None:
            try:
                look = self.port.lookup_order(s.symbol, client_oid)
            except Exception as exc:
                return self._block_unknown(f"order lookup failed: {type(exc).__name__}")
            status = str((look or {}).get("status", "UNKNOWN")).upper()
            if status == "ABSENT":
                self._phase("ABORTED", reason="entry definitively absent on exchange; no position")
                return self._result("ABORTED", reason="entry absent")
            if status == "FOUND":
                order = look.get("order") or {}
                self._phase("ENTRY_FOUND_NOT_FILLED", order_state=str(order.get("state") or order.get("status")))
                return self._block_unknown("order exists but no position confirmed within timeout; operator must reconcile")
            return self._block_unknown("entry outcome UNKNOWN (neither position nor definitive absence)")
        size = _f(pos.get("total")) or 0.0
        avg = _f(pos.get("openPriceAvg"))
        self._phase("FILL_CONFIRMED", size=size, avg_entry=avg, source="position_endpoint")

        # ---- PROTECTION (positions are never left unprotected on purpose)
        sign = 1 if s.direction == "LONG" else -1
        stop = st.get("stop") or ((avg or 0) * (1 - sign * s.stop_pct / 100))
        tp = st.get("take_profit") or ((avg or 0) * (1 + sign * s.take_profit_pct / 100))
        confirmed = False
        for attempt in (1, 2):
            try:
                prot = self.port.place_protection(s.symbol, s.direction, size, stop, [tp]) or {}
                confirmed = bool(prot.get("stop_loss_verified") or prot.get("protection_verified"))
            except Exception as exc:
                prot = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            self.j.write(self.run_id, "PROTECTION_ATTEMPT", attempt=attempt, confirmed=confirmed, detail={k: prot.get(k) for k in ("protection_verified", "stop_loss_verified", "protection_integrity", "error")})
            if confirmed:
                break
        if not confirmed:
            self._phase("PROTECTION_FAILED", action="fail_safe_close")
            return self._exit(size, reason="protection_unconfirmed_failsafe", failsafe=True)
        self._phase("PROTECTION_CONFIRMED", stop=stop, take_profit=tp)
        if s.hold_seconds > 0:
            self.sleep(s.hold_seconds)
        return self._exit(size, reason="smoke_exit", failsafe=False)

    def _exit(self, size: float, *, reason: str, failsafe: bool) -> dict[str, Any]:
        s = self.spec
        try:
            resp = self.port.close_full(s.symbol, s.direction, size, reason)
            self._phase("EXIT_SUBMITTED", reason=reason, ack=_ack(resp))
        except Exception as exc:
            self._phase("EXIT_SUBMIT_EXCEPTION", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        try:
            flat = self._wait(lambda: self._flat(s.symbol, s.direction), s.fill_timeout_s)
        except Exception:
            flat = None
        if not flat:
            self.emergency.engage(f"smoke run {self.run_id}: position NOT confirmed flat after close", by="smoke_lifecycle")
            self._phase("OPERATOR_ATTENTION", reason="close not confirmed; position may be open and/or unprotected; emergency stop engaged")
            return self._result("OPERATOR_ATTENTION", reason="close_not_confirmed")
        try:
            leftovers = self.port.plan_orders()
        except Exception:
            leftovers = None
        self._phase("FLAT_CONFIRMED", leftover_plan_orders=None if leftovers is None else len(leftovers))
        if leftovers:
            self.emergency.engage(f"smoke run {self.run_id}: orphan plan orders remain after flat", by="smoke_lifecycle")
            self._phase("OPERATOR_ATTENTION", reason="orphan TP/SL plan orders remain; emergency stop engaged")
            return self._result("OPERATOR_ATTENTION", reason="orphan_plan_orders")
        final = "FAILSAFE_CLOSED" if failsafe else "DONE"
        self._phase(final)
        return self._result(final)

    def _block_unknown(self, why: str) -> dict[str, Any]:
        self.emergency.engage(f"smoke run {self.run_id}: {why}", by="smoke_lifecycle")
        self._phase("BLOCKED_UNKNOWN", reason=why)
        return self._result("BLOCKED_UNKNOWN", reason=why)

    def _result(self, outcome: str, **extra: Any) -> dict[str, Any]:
        return {"run_id": self.run_id, "outcome": outcome, "spec_sha256": self.spec.sha256(),
                "journal": str(self.j.path), "journal_chain_valid": Journal.verify(self.j.path), **extra}

    # ---- restart reconciliation
    @classmethod
    def resume(cls, port: ExchangePort, *, state_path: str | os.PathLike, journal_path: str | os.PathLike, **kw: Any) -> dict[str, Any]:
        st = json.loads(Path(state_path).read_text())
        spec = SmokeSpec(**st["spec"])
        r = cls(port, spec, state_path=state_path, journal_path=journal_path, **kw)
        r.run_id, r.state = st.get("run_id", r.run_id), st
        ph = st.get("phase")
        if ph in TERMINAL or ph in {"DRY_RUN_COMPLETE", "START", "PRECHECK", "SIZING"} or not st.get("client_oid"):
            r.j.write(r.run_id, "RESUME_NOOP", was_phase=ph)
            return r._result(ph or "UNKNOWN", note="nothing to reconcile; no order was ever persisted")
        r.j.write(r.run_id, "RESUME_RECONCILE", from_phase=ph)
        # Never submits an entry again: reconciles exchange truth, protects, exits.
        return r._after_entry()


def _ack(resp: Any) -> dict[str, Any]:
    d = (resp or {}).get("data") if isinstance(resp, dict) else None
    d = d[0] if isinstance(d, list) and d else d
    return {"code": (resp or {}).get("code") if isinstance(resp, dict) else None, "orderId": (d or {}).get("orderId") if isinstance(d, dict) else None,
            "note": "ACK_IS_NOT_A_FILL"}
