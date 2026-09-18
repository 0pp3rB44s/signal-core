"""Mandatory, non-bypassable RiskManager authorization gate (Deployment Gap #1 fix).

REPRODUCED DEFECT (this mission, code-read confirmed): `execution/execution_service.py`'s
`ExecutionService.execute()` -- the single, strategy-agnostic order-submission entry point every
`TradePlan` flows through -- calls `RiskManager.evaluate()` nowhere. Risk authorization currently
happens only inside `planning/trade_planner.py`, which is the sanctioned TradePlan-construction
path for most strategies. `strategies/adaptive_trend_plan.py.build_trade_plan()` is a second,
parallel TradePlan-construction path that never calls `RiskManager.evaluate()` at all -- it
hardcodes `verdict="EXECUTABLE"`. Because `execute()` trusts any TradePlan handed to it, ANY
future path that constructs a TradePlan outside `TradePlanner` silently skips every RiskManager
gate (correlated-symbol/cluster-exposure, directional-exposure, AI-override, kill-switch).

Note on current blast radius, verified this mission: `strategies/adaptive_trend_runtime.py`'s
`route_selected_candidate()` never calls `build_trade_plan()` or `execute()` -- "this always ends
in a shadow record, never a real order" (its own docstring). So this defect is currently LATENT,
not currently reachable from any live order. It must still be closed structurally before any
strategy following this pattern (including a future live AdaptiveTrend wiring, or Turtle's
not-yet-built execution path) could accidentally inherit the same bypass.

THE FIX: a mandatory authorization gate that is independent of which module built the TradePlan.
  1. `authorize()` wraps `RiskManager.evaluate()` and persists a signed-by-content receipt keyed
     by the plan's own deterministic `candidate_id` -- the ONLY sanctioned way to produce a
     recorded authorization.
  2. `require_authorized()` is called by `ExecutionService.execute()` (integration point, not
     wired into production by this mission -- see MICRO_LIVE_ENGINEERING_GAP_FIXES.md) FIRST, for
     EVERY plan, unconditionally. No matching, ALLOWED, content-matching receipt -> hard refusal.
     This is strategy-agnostic: it does not special-case AdaptiveTrend or any other name.
  3. Fails closed on every uncertain condition: RiskManager raising, a missing/corrupt store file,
     a receipt whose recorded plan content no longer matches the plan being executed (replay/
     tamper guard), or process restart with no receipt on disk -- all block, none default-allow.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("risk.mandatory_authorization")

DEFAULT_STORE_PATH = "state/risk_authorizations.json"
AUTHORIZATION_TTL_SECONDS = 300.0  # an authorization is only valid for a short window after
                                     # issuance -- prevents a stale ALLOW from a much earlier,
                                     # differently-risked moment being reused for a later submit


class RiskAuthorizationError(RuntimeError):
    """Base class: any reason execution must not proceed."""


class RiskManagerUnavailable(RiskAuthorizationError):
    pass


class RiskAuthorizationMissing(RiskAuthorizationError):
    pass


class RiskAuthorizationExpired(RiskAuthorizationError):
    pass


class RiskAuthorizationMismatch(RiskAuthorizationError):
    """The plan being executed does not match what was actually authorized."""


class RiskStateCorrupt(RiskAuthorizationError):
    pass


def _plan_content_hash(*, candidate_id: str, plan_id: str, symbol: str, direction: str,
                        position_notional_usdt: float, leverage: float) -> str:
    """A content fingerprint tying an authorization to the EXACT plan economics that were
    evaluated -- a plan_id match alone would not catch a plan whose size/leverage was mutated
    after risk evaluation but before submission."""
    material = json.dumps(
        ["risk-authorization-v1", candidate_id, plan_id, symbol.upper(), direction.upper(),
         round(float(position_notional_usdt), 8), round(float(leverage), 8)],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class RiskAuthorization:
    candidate_id: str
    plan_id: str
    allowed: bool
    reasons: list[str]
    content_hash: str
    evaluated_at: float  # time.time()
    account_risk_pct: float
    leverage: float
    max_open_positions: int

    def is_expired(self, *, now: float | None = None, ttl: float = AUTHORIZATION_TTL_SECONDS) -> bool:
        return (now or time.time()) - self.evaluated_at > ttl

    def to_dict(self) -> dict[str, Any]:
        return dict(candidate_id=self.candidate_id, plan_id=self.plan_id, allowed=self.allowed,
                    reasons=list(self.reasons), content_hash=self.content_hash,
                    evaluated_at=self.evaluated_at, account_risk_pct=self.account_risk_pct,
                    leverage=self.leverage, max_open_positions=self.max_open_positions)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskAuthorization":
        return cls(candidate_id=d["candidate_id"], plan_id=d["plan_id"], allowed=bool(d["allowed"]),
                    reasons=list(d.get("reasons") or []), content_hash=d["content_hash"],
                    evaluated_at=float(d["evaluated_at"]), account_risk_pct=float(d["account_risk_pct"]),
                    leverage=float(d["leverage"]), max_open_positions=int(d["max_open_positions"]))


class RiskAuthorizationStore:
    """Restart-safe, append-then-compact JSON store keyed by candidate_id.

    Atomic write (temp file + os.replace), matching this project's established
    `execution/state_store.py` convention -- not reinvented, reused deliberately.
    """

    def __init__(self, path: str = DEFAULT_STORE_PATH) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise RiskStateCorrupt(f"authorization store unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise RiskStateCorrupt("authorization store is not a JSON object")
        return data

    def put(self, auth: RiskAuthorization) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load()
        data[auth.candidate_id] = auth.to_dict()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def get(self, candidate_id: str) -> RiskAuthorization | None:
        data = self._load()  # RiskStateCorrupt propagates -- fail closed, never treat as "no record"
        row = data.get(candidate_id)
        if row is None:
            return None
        try:
            return RiskAuthorization.from_dict(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStateCorrupt(f"authorization record for {candidate_id} is malformed: {exc}") from exc


def _build_and_store(
    verdict,  # clients.schemas.RiskVerdict -- already computed
    *,
    candidate_id: str,
    plan_id: str,
    symbol: str,
    direction: str,
    position_notional_usdt: float,
    store: RiskAuthorizationStore,
) -> "RiskAuthorization":
    """Shared core for authorize() and record_authorization(): turn an already-computed
    RiskVerdict into a persisted receipt bound to the EXACT plan economics it will be checked
    against later. Not exported -- reached only via the two public entry points below, so every
    stored authorization traces back to a genuine RiskManager.evaluate() call, never a
    fabricated one.

    Reads `allowed` and `reasons` as required (the actual authorization decision
    depends on them and must not silently default). `leverage`, `account_risk_pct`,
    and `max_open_positions` are read defensively (getattr with a safe default):
    they are descriptive/audit fields, not part of the allow/deny decision itself,
    and some legitimate callers construct a minimal verdict-like stand-in (e.g. a
    bare SimpleNamespace in tests exercising an unrelated concern) that sets
    allowed/reasons but not every RiskVerdict field. Defaulting them never makes a
    denied verdict look allowed, or vice versa -- only the recorded audit detail
    is coarser in that case."""
    leverage = getattr(verdict, "leverage", 0.0)
    content_hash = _plan_content_hash(
        candidate_id=candidate_id, plan_id=plan_id, symbol=symbol, direction=direction,
        position_notional_usdt=position_notional_usdt, leverage=leverage,
    )
    auth = RiskAuthorization(
        candidate_id=candidate_id, plan_id=plan_id, allowed=bool(verdict.allowed),
        reasons=list(getattr(verdict, "reasons", None) or []), content_hash=content_hash,
        evaluated_at=time.time(), account_risk_pct=getattr(verdict, "account_risk_pct", 0.0),
        leverage=leverage, max_open_positions=getattr(verdict, "max_open_positions", 0),
    )
    store.put(auth)
    log.warning("RISK_AUTHORIZATION_RECORDED | candidate_id=%s | plan_id=%s | allowed=%s | reasons=%s",
               candidate_id, plan_id, auth.allowed, auth.reasons)
    return auth


def authorize(
    risk_manager,  # risk.risk_manager.RiskManager -- typed loosely to avoid a hard import cycle
    candidate,     # clients.schemas.StrategyCandidate
    score,         # clients.schemas.StrategyScore
    *,
    plan_id: str,
    position_notional_usdt: float,
    store: RiskAuthorizationStore,
    observed_equity: float | None = None,
) -> "RiskAuthorization":
    """Call RiskManager.evaluate() and record the result in one step. Use this when nothing has
    evaluated risk for this candidate yet.

    NOT for the app/runner.py shape, where a RiskVerdict must already exist before
    TradePlanner.build() can run -- use record_authorization() there instead, with the SAME
    already-computed verdict, so RiskManager is never evaluated twice for one candidate.

    Fails closed: any exception from RiskManager.evaluate() itself propagates as
    RiskManagerUnavailable and NOTHING is persisted (so require_authorized() below correctly
    finds nothing and refuses)."""
    try:
        verdict = risk_manager.evaluate(candidate, score, observed_equity=observed_equity,
                                         proposed_notional_usdt=position_notional_usdt)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: ANY risk-manager failure must
                                # block, not just the exception types anticipated today
        log.error("RISK_MANAGER_UNAVAILABLE | candidate_id=%s | %s", candidate.candidate_id, exc)
        raise RiskManagerUnavailable(str(exc)) from exc

    return _build_and_store(
        verdict, candidate_id=candidate.candidate_id, plan_id=plan_id, symbol=candidate.symbol,
        direction=candidate.direction, position_notional_usdt=position_notional_usdt, store=store,
    )


def record_authorization(
    verdict,     # clients.schemas.RiskVerdict -- already computed by an earlier risk_manager.evaluate()
    candidate,   # clients.schemas.StrategyCandidate
    *,
    plan_id: str,
    position_notional_usdt: float,
    store: RiskAuthorizationStore,
) -> "RiskAuthorization":
    """Record a RiskVerdict that was already obtained via a real RiskManager.evaluate() call
    elsewhere -- the app/runner.py shape, where evaluate() must run BEFORE TradePlanner.build()
    (build() needs the verdict to compute its own EXECUTABLE/BLOCKED decision) but a plan's final
    notional/leverage are only known AFTER build() returns. Use the SAME verdict object that was
    passed into build(), so RiskManager is never evaluated twice for one candidate.

    Does NOT call RiskManager itself -- this is not a way to skip evaluation, only a way to record
    one that genuinely already happened. Callers are this project's own strategy-runner code, not
    external input, so the verdict is trusted rather than independently re-derived here."""
    return _build_and_store(
        verdict, candidate_id=candidate.candidate_id, plan_id=plan_id, symbol=candidate.symbol,
        direction=candidate.direction, position_notional_usdt=position_notional_usdt, store=store,
    )


def require_authorized(
    plan,  # clients.schemas.TradePlan
    *,
    store: RiskAuthorizationStore,
    now: float | None = None,
    ttl: float = AUTHORIZATION_TTL_SECONDS,
) -> RiskAuthorization:
    """Call this FIRST, unconditionally, for every plan, in ExecutionService.execute() (or
    wherever real order submission begins) -- before sizing/precision/order-intent, before
    anything that could create an exchange order. Strategy-agnostic: reads only plan.candidate_id
    and plan's own economics, never plan.strategy, so there is no name to special-case around.

    Raises (never returns a "denied but proceed" value) on: no record found, RiskStateCorrupt
    (propagated from the store), an expired record, a content mismatch (the plan was mutated
    after authorization), or an explicit allowed=False verdict.
    """
    auth = store.get(plan.candidate_id)  # RiskStateCorrupt propagates unchanged -- fail closed
    if auth is None:
        raise RiskAuthorizationMissing(
            f"no RiskManager authorization on record for candidate_id={plan.candidate_id!r} "
            f"(plan_id={plan.plan_id!r}); every TradePlan must be authorized via "
            f"risk.mandatory_authorization.authorize() before execute() may act on it"
        )
    if auth.is_expired(now=now, ttl=ttl):
        raise RiskAuthorizationExpired(
            f"authorization for candidate_id={plan.candidate_id!r} is "
            f"{(now or time.time()) - auth.evaluated_at:.1f}s old, exceeds {ttl:.0f}s TTL"
        )
    expected_hash = _plan_content_hash(
        candidate_id=plan.candidate_id, plan_id=plan.plan_id, symbol=plan.symbol,
        direction=plan.direction, position_notional_usdt=plan.position_notional_usdt,
        leverage=plan.leverage,
    )
    if expected_hash != auth.content_hash:
        raise RiskAuthorizationMismatch(
            f"plan content for candidate_id={plan.candidate_id!r} does not match what was "
            f"authorized (economics changed after risk evaluation) -- refusing to execute"
        )
    if not auth.allowed:
        raise RiskAuthorizationMissing(  # deliberately the same exception family: "not clear to act"
            f"RiskManager explicitly denied candidate_id={plan.candidate_id!r}: {auth.reasons}"
        )
    return auth


__all__ = [
    "RiskAuthorization", "RiskAuthorizationStore", "authorize", "record_authorization", "require_authorized",
    "RiskAuthorizationError", "RiskManagerUnavailable", "RiskAuthorizationMissing",
    "RiskAuthorizationExpired", "RiskAuthorizationMismatch", "RiskStateCorrupt",
    "AUTHORIZATION_TTL_SECONDS",
]
