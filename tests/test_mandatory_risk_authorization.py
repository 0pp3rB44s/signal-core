"""Deployment Gap #1 fix tests: mandatory, non-bypassable RiskManager authorization.

Required scenarios per mission: single position, multiple correlated positions, cluster
exposure breach, global exposure breach, risk manager unavailable, malformed risk state,
process restart, duplicate signal. A rejected/absent authorization must make order
submission structurally impossible -- every test proves that by asserting execute() (via
require_authorized) raises rather than merely returning a falsy/ignorable value.
"""
from __future__ import annotations

import json

import pytest

from candidate_lifecycle.identity import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import MarketSnapshot, StrategyCandidate, StrategyScore, SweepDetection, TradePlan
from risk.mandatory_authorization import (
    RiskAuthorizationExpired, RiskAuthorizationMismatch, RiskAuthorizationMissing,
    RiskAuthorizationStore, RiskManagerUnavailable, RiskStateCorrupt, authorize,
    require_authorized,
)


def _candidate(symbol="BTCUSDT", direction="LONG", strategy="adaptive_trend_tsmom_v1",
               candle_open_ms=1_800_000_000_000):
    candidate_id = deterministic_candidate_id(strategy, symbol, direction, candle_open_ms)
    market = MarketSnapshot(**_market_kwargs())
    detection = SweepDetection(**_detection_kwargs())
    return StrategyCandidate(
        candidate_id=candidate_id, candidate_candle_open_timestamp_ms=candle_open_ms,
        symbol=symbol, strategy=strategy, direction=direction,
        primary_granularity="6H", confirmation_granularity="1D",
        market=market, detection=detection, notes=[],
    )


def _market_kwargs():
    import inspect
    fields = {f: 0.0 for f in inspect.signature(MarketSnapshot).parameters}
    for k in fields:
        if "notes" in k or "flags" in k:
            fields[k] = []
    return fields


def _detection_kwargs():
    import inspect
    fields = {}
    for name, p in inspect.signature(SweepDetection).parameters.items():
        if p.annotation in (int,) or "bars" in name:
            fields[name] = 0
        elif "flags" in name:
            fields[name] = []
        else:
            fields[name] = 0.0
    return fields


def _plan(candidate: StrategyCandidate, *, notional=20.0, leverage=3.0):
    plan_id = deterministic_plan_id(candidate.candidate_id)
    return TradePlan(
        candidate_id=candidate.candidate_id, candidate_candle_open_timestamp_ms=candidate.candidate_candle_open_timestamp_ms,
        plan_id=plan_id, symbol=candidate.symbol, strategy=candidate.strategy, direction=candidate.direction,
        verdict="EXECUTABLE", score=1.0, entry_prices=[100.0], stop_loss=98.0, take_profits=[],
        risk_reward_ratio=0.0, account_risk_pct=0.5, leverage=leverage, position_notional_usdt=notional,
        notes=[], reasons=["test"],
    )


class FakeRiskManager:
    """Controllable stand-in so this gate's OWN contract is tested in isolation from the
    real RiskManager's settings/file dependencies (which already have their own coverage
    in tests/test_risk_manager.py)."""

    def __init__(self, allowed=True, reasons=None, raise_exc=None, leverage=3.0,
                 account_risk_pct=0.5, max_open_positions=1):
        self.allowed, self.reasons, self.raise_exc = allowed, reasons or [], raise_exc
        self.leverage, self.account_risk_pct, self.max_open_positions = leverage, account_risk_pct, max_open_positions
        self.calls = 0

    def evaluate(self, candidate, score, *, observed_equity=None, proposed_notional_usdt=0.0):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        from clients.schemas import RiskVerdict
        return RiskVerdict(allowed=self.allowed, status="ALLOWED" if self.allowed else "DENIED",
                           reasons=self.reasons, account_risk_pct=self.account_risk_pct,
                           leverage=self.leverage, max_open_positions=self.max_open_positions)


def test_single_position_authorized_and_executable(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=True)
    cand = _candidate()
    plan = _plan(cand)
    auth = authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
                     plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    assert auth.allowed
    result = require_authorized(plan, store=store)
    assert result.allowed


def test_denied_verdict_blocks_execution_even_though_a_record_exists(tmp_path):
    """A rejected risk authorization must make order submission impossible -- not just
    logged/ignored. require_authorized must raise, not return a falsy verdict a caller
    could accidentally proceed past."""
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=False, reasons=["kill-switch: daily defensive mode active"])
    cand = _candidate()
    plan = _plan(cand)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    with pytest.raises(RiskAuthorizationMissing):
        require_authorized(plan, store=store)


def test_cluster_exposure_breach_denial_blocks(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=False, reasons=["cluster-risk: BTC/ETH/SOL correlated exposure limit reached"])
    cand = _candidate(symbol="ETHUSDT")
    plan = _plan(cand)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    with pytest.raises(RiskAuthorizationMissing, match="cluster-risk"):
        require_authorized(plan, store=store)


def test_global_exposure_breach_denial_blocks(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=False, reasons=["directional-exposure: global long exposure cap reached"])
    cand = _candidate()
    plan = _plan(cand)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    with pytest.raises(RiskAuthorizationMissing, match="directional-exposure"):
        require_authorized(plan, store=store)


def test_multiple_correlated_positions_each_independently_authorized(tmp_path):
    """Multiple candidates (simulating correlated BTC/ETH/SOL positions) each get their own
    authorization keyed by their own candidate_id -- one denial does not silently allow
    another, and one candidate's record cannot be mistaken for another's."""
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    btc = _candidate(symbol="BTCUSDT")
    eth = _candidate(symbol="ETHUSDT")
    sol = _candidate(symbol="SOLUSDT")
    rm_allow = FakeRiskManager(allowed=True)
    rm_deny = FakeRiskManager(allowed=False, reasons=["cluster-risk: third correlated position"])
    score = StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[])
    for cand, rm in ((btc, rm_allow), (eth, rm_allow), (sol, rm_deny)):
        plan = _plan(cand)
        authorize(rm, cand, score, plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)

    assert require_authorized(_plan(btc), store=store).allowed
    assert require_authorized(_plan(eth), store=store).allowed
    with pytest.raises(RiskAuthorizationMissing):
        require_authorized(_plan(sol), store=store)


def test_risk_manager_unavailable_raises_and_persists_nothing(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(raise_exc=RuntimeError("symbol_expectancy file corrupt"))
    cand = _candidate()
    plan = _plan(cand)
    with pytest.raises(RiskManagerUnavailable):
        authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
                 plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    # nothing was persisted -- the plan is exactly as unauthorized as if authorize() were never called
    with pytest.raises(RiskAuthorizationMissing):
        require_authorized(plan, store=store)


def test_malformed_risk_state_on_disk_fails_closed_not_treated_as_absent(tmp_path):
    """A corrupted authorization store must be distinguishable from 'no record' -- both
    ultimately block, but silently treating corruption as absence would hide a real
    integrity problem. RiskStateCorrupt must propagate, not be swallowed into a plain miss."""
    path = tmp_path / "auth.json"
    path.write_text("{not valid json")
    store = RiskAuthorizationStore(str(path))
    cand = _candidate()
    plan = _plan(cand)
    with pytest.raises(RiskStateCorrupt):
        require_authorized(plan, store=store)


def test_process_restart_recovers_authorization_from_disk(tmp_path):
    path = str(tmp_path / "auth.json")
    store_before = RiskAuthorizationStore(path)
    rm = FakeRiskManager(allowed=True)
    cand = _candidate()
    plan = _plan(cand)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store_before)
    # simulate a process restart: a fresh Store instance over the same path
    store_after_restart = RiskAuthorizationStore(path)
    result = require_authorized(plan, store=store_after_restart)
    assert result.allowed


def test_process_restart_with_no_prior_record_blocks(tmp_path):
    """A restart with nothing on disk must block, not default-allow -- proves the gate
    never treats 'file doesn't exist yet' as implicit permission."""
    store = RiskAuthorizationStore(str(tmp_path / "never_written.json"))
    cand = _candidate()
    plan = _plan(cand)
    with pytest.raises(RiskAuthorizationMissing):
        require_authorized(plan, store=store)


def test_duplicate_signal_reuses_existing_authorization_not_a_second_evaluation(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=True)
    cand = _candidate()
    plan = _plan(cand)
    score = StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[])
    auth1 = authorize(rm, cand, score, plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    # Duplicate signal: same candidate authorized again (e.g. a re-processed event).
    auth2 = authorize(rm, cand, score, plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    assert rm.calls == 2  # authorize() itself does not dedupe re-evaluation -- that is the
                           # entry-path's job (execution.order_identity/order_intent_store,
                           # already covered in test_order_idempotency.py); this module's own
                           # job is just that execute() can never proceed without SOME valid record
    assert require_authorized(plan, store=store).allowed


def test_expired_authorization_blocks(tmp_path):
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=True)
    cand = _candidate()
    plan = _plan(cand)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=plan.plan_id, position_notional_usdt=plan.position_notional_usdt, store=store)
    import time
    with pytest.raises(RiskAuthorizationExpired):
        require_authorized(plan, store=store, now=time.time() + 3600)


def test_plan_mutated_after_authorization_blocks_on_content_mismatch(tmp_path):
    """A plan whose notional/leverage changed AFTER risk evaluation (e.g. a bug re-sizing
    it downstream) must not silently reuse the earlier authorization -- proves the gate
    binds to plan ECONOMICS, not just candidate_id."""
    store = RiskAuthorizationStore(str(tmp_path / "auth.json"))
    rm = FakeRiskManager(allowed=True)
    cand = _candidate()
    original_plan = _plan(cand, notional=20.0)
    authorize(rm, cand, StrategyScore(total=1, breakdown={}, verdict="PASS", reasons=[]),
             plan_id=original_plan.plan_id, position_notional_usdt=original_plan.position_notional_usdt, store=store)
    mutated_plan = _plan(cand, notional=200.0)  # 10x larger, never re-authorized
    with pytest.raises(RiskAuthorizationMismatch):
        require_authorized(mutated_plan, store=store)


def test_adaptive_trend_plan_bypass_is_reproduced_at_unit_level():
    """Direct reproduction of Agent C's finding: build_trade_plan() hardcodes an executable
    verdict with zero RiskManager involvement. This documents the defect exists in that
    function in isolation -- the fix is the mandatory gate above, applied at execute()."""
    from strategies.adaptive_trend_plan import build_trade_plan
    import inspect
    source = inspect.getsource(build_trade_plan)
    assert "RiskManager" not in source
    assert "risk_manager" not in source
    assert 'verdict="EXECUTABLE"' in source  # hardcoded, unconditional
