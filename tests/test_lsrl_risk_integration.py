"""LSRL_V1 x the CENTRAL RiskManager: it passes the gate, and every central protective gate still bites."""
from __future__ import annotations

import pytest

import app.equity as equity_mod
from app.config import Settings
from lsrl.adapters.riskgate import CentralRiskGate, build_candidate
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import RiskProposal
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.types import Direction, PlaceEntry
from risk import symbol_expectancy
from risk.risk_manager import RiskManager
from tests.lsrl_fixtures import RULES, Harness, long_sweep, mirror


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(equity_mod, "EQUITY_SNAPSHOT_PATH", tmp_path / "no_equity.json")
    monkeypatch.setattr(symbol_expectancy, "DATASET_PATH", tmp_path / "no_trade_dataset.csv")
    symbol_expectancy.reset_cache()


def _rm(*, daily=None, weekly=0.0, decisions=(), open_positions=(), **settings) -> RiskManager:
    s = Settings(_env_file=None, **settings)
    rm = RiskManager(settings=s)
    rm._latest_backtest_summary = lambda: {}
    rm._latest_strategy_expectancy = lambda: {}
    rm._weekly_realized_pnl = lambda: weekly
    rm._daily_defensive_status = lambda: daily or {"daily_total_net_pnl": 0.0, "consecutive_losses": 0}
    rm._latest_agent_decisions = staticmethod(lambda: {"decisions": list(decisions)})
    rm._load_open_positions = staticmethod(lambda: list(open_positions) if open_positions is not None else None)
    # The central manager sizes DOWN inside certain UTC hour windows (23:00 UTC was one of them when these tests were
    # first run at a different hour). Pin a neutral hour so the assertions do not depend on the wall clock.
    real_session = rm._session_risk_multiplier
    rm._session_risk_multiplier = lambda now_hour_utc=None: real_session(now_hour_utc=14 if now_hour_utc is None else now_hour_utc)
    return rm


def _proposal(direction=Direction.LONG, notional=200.0, nav=1_000.0) -> RiskProposal:
    prices = (149.6, 149.5, 149.4, 149.3, 149.2) if direction is Direction.LONG else (150.4, 150.5, 150.6, 150.7, 150.8)
    return RiskProposal(setup_id="s1", symbol="SOLUSDT", direction=direction, t_ms=1_800_000_000_000,
                        nav_usdt=nav, entry_prices=prices, stop=149.13 if direction is Direction.LONG else 150.87,
                        total_qty=1.0, planned_notional_usdt=notional, planned_loss_usdt=nav * 0.005,
                        strategy_risk_fraction=0.005, spread_bps=3.0)


def _evaluate(rm: RiskManager, **kw):
    gate = CentralRiskGate(rm, rm.settings)
    return gate, gate.check(_proposal(**kw))


# ------------------------------------------------------------------ passes the central gate
def test_lsrl_long_and_short_pass_the_unmodified_central_risk_manager():
    for direction in (Direction.LONG, Direction.SHORT):
        gate, decision = _evaluate(_rm(), direction=direction)
        assert decision.allowed, decision.reasons
        assert decision.source == "RiskManager"
        assert any("lsrl_v1: frozen liquidity-sweep entry policy" in r for r in decision.reasons)
        assert not any("Safe Mode blocks unsupported strategy" in r for r in decision.reasons)


def test_an_unknown_strategy_name_is_still_blocked_lsrl_is_not_a_wildcard():
    rm = _rm()
    cand = build_candidate(_proposal())
    cand.strategy = "lsrl_v2"
    from lsrl.adapters.riskgate import FROZEN_SCORE
    verdict = rm.evaluate(cand, FROZEN_SCORE, observed_equity=1_000.0, proposed_notional_usdt=200.0)
    assert not verdict.allowed and any("Safe Mode blocks unsupported strategy" in r for r in verdict.reasons)


def test_the_legacy_htf_alignment_gate_does_not_veto_lsrl_but_still_vetoes_other_strategies():
    rm = _rm()
    from lsrl.adapters.riskgate import FROZEN_SCORE
    lsrl = build_candidate(_proposal())
    lsrl.market.alignment = "aligned_bearish"                      # a LONG against the HTF: legacy gate would veto
    assert rm.evaluate(lsrl, FROZEN_SCORE, observed_equity=1_000.0, proposed_notional_usdt=200.0).allowed
    other = build_candidate(_proposal())
    other.strategy = "low_vol_reclaim"
    other.market.alignment = "aligned_bearish"
    verdict = rm.evaluate(other, FROZEN_SCORE, observed_equity=1_000.0, proposed_notional_usdt=200.0)
    assert not verdict.allowed and any("HTF alignment opposes long setup" in r for r in verdict.reasons)


# ------------------------------------------------------------------ every central gate still bites
def test_daily_defensive_stop_blocks_lsrl():
    rm = _rm(daily={"daily_total_net_pnl": -30.0, "consecutive_losses": 0}, HARD_DAILY_STOP_PCT=2.0)
    _gate, decision = _evaluate(rm)
    assert not decision.allowed and any("kill-switch: daily defensive mode active" in r for r in decision.reasons)


def test_consecutive_loss_limit_blocks_lsrl():
    rm = _rm(daily={"daily_total_net_pnl": 0.0, "consecutive_losses": 3})
    _gate, decision = _evaluate(rm)
    assert not decision.allowed and any("consecutive loss limit" in r for r in decision.reasons)


def test_weekly_freeze_blocks_lsrl():
    rm = _rm(weekly=-100.0, WEEKLY_FREEZE_LOSS_PCT=7.0)
    _gate, decision = _evaluate(rm)
    assert not decision.allowed and any("weekly freeze active" in r for r in decision.reasons)


def test_shorts_switch_is_authoritative_for_lsrl():
    _g, allowed = _evaluate(_rm(ENABLE_SHORTS=True), direction=Direction.SHORT)
    _g, blocked = _evaluate(_rm(ENABLE_SHORTS=False), direction=Direction.SHORT)
    assert allowed.allowed and not blocked.allowed
    assert any("shorts disabled by configuration" in r for r in blocked.reasons)
    assert _evaluate(_rm(ENABLE_SHORTS=False), direction=Direction.LONG)[1].allowed


def test_enabled_strategies_allow_list_is_respected():
    _g, decision = _evaluate(_rm(ENABLED_STRATEGIES="adaptive_trend_tsmom_v1"))
    assert not decision.allowed and any("not in ENABLED_STRATEGIES allow-list" in r for r in decision.reasons)
    assert _evaluate(_rm(ENABLED_STRATEGIES="lsrl_v1"))[1].allowed


def test_unreadable_open_position_state_fails_closed_for_the_solusdt_cluster():
    _g, decision = _evaluate(_rm(open_positions=None))
    assert not decision.allowed and any("open-position state unreadable" in r for r in decision.reasons)


def test_coach_symbol_avoidance_blocks_lsrl():
    rm = _rm(decisions=[{"action": "avoid_symbol_until_improved", "target": "SOLUSDT", "reason": "test"}])
    _g, decision = _evaluate(rm)
    assert not decision.allowed and any("HARD_BLOCK: symbol avoided by coach" in r for r in decision.reasons)


def test_wide_spread_is_vetoed_by_the_central_execution_cost_gate_too():
    rm = _rm()
    gate = CentralRiskGate(rm, rm.settings)
    fields = {f: getattr(_proposal(), f) for f in RiskProposal.__dataclass_fields__}
    assert not gate.check(RiskProposal(**{**fields, "spread_bps": 9.0})).allowed


# ------------------------------------------------------------------ the gate only ever reduces risk
def test_central_probe_or_session_multiplier_reduces_the_applied_risk_fraction():
    rm = _rm(ACCOUNT_RISK_PER_TRADE_PCT=0.3)                           # central 0.3% < LSRL's 0.5%
    _g, decision = _evaluate(rm)
    assert decision.allowed and decision.applied_risk_fraction == pytest.approx(0.003)


def test_central_risk_above_the_strategy_fraction_never_raises_it():
    _g, decision = _evaluate(_rm(ACCOUNT_RISK_PER_TRADE_PCT=0.75))
    assert decision.applied_risk_fraction == pytest.approx(0.005)


def test_planned_notional_above_the_leverage_ceiling_scales_risk_down():
    rm = _rm(DEFAULT_LEVERAGE=5.0, MAX_LEVERAGE=5.0, MAX_TOTAL_EXPOSURE_PCT=10_000.0, MAX_CLUSTER_EXPOSURE_PCT=10_000.0)
    _g, decision = _evaluate(rm, notional=10_000.0, nav=1_000.0)      # leverage ceiling 1000*5 = 5000
    assert decision.allowed and decision.applied_risk_fraction == pytest.approx(0.005 * 5_000 / 10_000)
    assert any("risk_scaled_to_fit_central_limits" in r for r in decision.reasons)


def test_planned_notional_is_scaled_into_the_portfolio_exposure_room():
    held = [{"symbol": "BTCUSDT", "position_notional_usdt": 500.0, "status": "OPEN", "direction": "LONG"}]
    rm = _rm(open_positions=held, MAX_TOTAL_EXPOSURE_PCT=200.0, MAX_CLUSTER_EXPOSURE_PCT=10_000.0)
    _g, decision = _evaluate(rm, notional=3_000.0, nav=1_000.0)       # room = 2000 - 500 = 1500
    assert decision.allowed, decision.reasons
    assert decision.applied_risk_fraction == pytest.approx(0.005 * 1_500 * 0.999 / 3_000)


def test_planned_notional_is_scaled_into_the_cluster_exposure_room():
    held = [{"symbol": "AVAXUSDT", "position_notional_usdt": 400.0, "status": "OPEN", "direction": "LONG"}]
    rm = _rm(open_positions=held, MAX_CLUSTER_EXPOSURE_PCT=120.0, MAX_TOTAL_EXPOSURE_PCT=10_000.0)
    _g, decision = _evaluate(rm, notional=3_000.0, nav=1_000.0)       # cluster room = 1200 - 400 = 800
    assert decision.allowed, decision.reasons
    assert decision.applied_risk_fraction == pytest.approx(0.005 * 800 * 0.999 / 3_000)


def test_a_proposal_that_already_fits_is_never_scaled():
    _g, decision = _evaluate(_rm(), notional=200.0, nav=1_000.0)
    assert decision.applied_risk_fraction == pytest.approx(0.005)
    assert not any("risk_scaled_to_fit_central_limits" in r for r in decision.reasons)


def test_live_flat_per_trade_notional_cap_applies_only_when_live():
    class Live:                                                        # a live-execution view of the same settings
        def __init__(self, base):
            self.__dict__.update({k: getattr(base, k) for k in ("default_leverage", "max_leverage")})
            self.execution_max_live_notional_per_trade_usdt = 35.0
            self.is_live_execution = True
    rm = _rm()
    gate = CentralRiskGate(rm, Live(rm.settings))
    decision = gate.check(_proposal(notional=350.0, nav=1_000.0))
    assert decision.applied_risk_fraction == pytest.approx(0.005 * 35 / 350)
    paper = CentralRiskGate(rm, rm.settings).check(_proposal(notional=350.0, nav=1_000.0))
    assert paper.applied_risk_fraction == pytest.approx(0.005)


def test_the_evaluated_verdict_is_retained_for_the_mandatory_authorization_record():
    rm = _rm()
    gate = CentralRiskGate(rm, rm.settings)
    gate.check(_proposal())
    verdict, candidate = gate.take_verdict("s1")
    assert verdict.allowed and candidate.strategy == "lsrl_v1" and candidate.symbol == "SOLUSDT"
    assert gate.take_verdict("s1") is None


# ------------------------------------------------------------------ through the engine
def test_engine_with_the_real_central_gate_places_the_ladder_and_persists_the_verdict():
    rm = _rm()
    h = Harness(risk_gate=CentralRiskGate(rm, rm.settings)).run(long_sweep())
    assert h.engine.setup is not None and len(h.intents_of(PlaceEntry)) == 5
    assert h.engine.setup.risk_gate["source"] == "RiskManager" and h.engine.setup.risk_gate["allowed"]


def test_engine_rejects_and_records_when_the_central_gate_blocks():
    rm = _rm(ENABLE_SHORTS=False)
    h = Harness(risk_gate=CentralRiskGate(rm, rm.settings)).run(mirror(long_sweep()))
    assert h.engine.setup is None and h.intents == [] and "RISK_MANAGER_REJECTED" in h.rejected()
    assert any("shorts disabled" in r for r in h.kinds("setup_rejected")[-1].data["risk_gate"]["reasons"])


def test_engine_shrinks_the_ladder_to_fit_the_central_notional_ceiling():
    rm = _rm(DEFAULT_LEVERAGE=1.0, MAX_LEVERAGE=1.0)                   # ceiling = NAV x 1
    h = Harness(risk_gate=CentralRiskGate(rm, rm.settings)).run(long_sweep())
    s = h.engine.setup
    assert s is not None
    planned = sum(s.sizing["rung_qty"] * p for p in s.geometry["rungs"])
    assert planned <= 1_000.0 + 1e-6 and s.sizing["risk_fraction"] < 0.005
