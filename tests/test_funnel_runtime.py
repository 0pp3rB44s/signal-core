"""build_funnel_runtime / build_funnel_fleet: the funnel restoration wired through LSRL_V1's own
LSRLRuntime shell (BitgetWsFeed, CandleFeed, LSRLRecorder, EventBus) - never a real socket or REST call in
these tests (FakeCandleClient/MetaClient are the same fakes LSRL_V1's own runtime tests use), never MICRO_LIVE.
"""
from __future__ import annotations

import pytest

from lsrl.driver import SimDriver
from lsrl.modes import Mode
from risk.risk_manager import RiskManager
from app.config import Settings
from tests.test_lsrl_feed_and_runtime import MetaClient, spec

from funnel.core.config import OWNER_APPROVED_PRODUCTION_SYMBOLS
from funnel.core.engine import FunnelEngine
from funnel.runtime import build_funnel_fleet, build_funnel_runtime


def _settings(**kw):
    return Settings(**kw)


def test_build_funnel_runtime_wires_a_paper_runtime_with_real_exchange_metadata(tmp_path):
    settings = _settings()
    rm = RiskManager(Settings())
    rt = build_funnel_runtime(settings, symbol="SOLUSDT", client=MetaClient(spec()), risk_manager=rm,
                              mode=Mode.PAPER, project_dir=tmp_path)
    assert rt.mode is Mode.PAPER
    assert isinstance(rt.driver, SimDriver)
    assert isinstance(rt.engine, FunnelEngine)
    assert rt.engine.rules.tick_size == 0.001            # from the (fake) exchange metadata, not hard-coded
    assert rt.engine.cfg.symbol == "SOLUSDT"
    assert rt.engine.risk_gate.risk_manager is rm


def test_build_funnel_runtime_refuses_micro_live():
    settings = _settings()
    rm = RiskManager(Settings())
    with pytest.raises(ValueError, match="only builds SHADOW/PAPER"):
        build_funnel_runtime(settings, symbol="SOLUSDT", client=MetaClient(spec()), risk_manager=rm,
                             mode=Mode.MICRO_LIVE)


def test_build_funnel_fleet_builds_the_full_owner_approved_symbol_universe_sharing_one_risk_manager(tmp_path):
    settings = _settings()
    rm = RiskManager(Settings())
    fleet = build_funnel_fleet(settings, client=MetaClient(spec()), risk_manager=rm, project_dir=tmp_path)
    assert set(fleet.keys()) == set(OWNER_APPROVED_PRODUCTION_SYMBOLS)
    assert len(fleet) == 8
    for sym, rt in fleet.items():
        assert rt.engine.cfg.symbol == sym
        assert rt.engine.risk_gate.risk_manager is rm      # every symbol shares the SAME RiskManager instance


def test_build_funnel_fleet_accepts_a_symbol_subset():
    settings = _settings()
    rm = RiskManager(Settings())
    fleet = build_funnel_fleet(settings, client=MetaClient(spec()), risk_manager=rm, symbols=("SOLUSDT", "BTCUSDT"))
    assert set(fleet.keys()) == {"SOLUSDT", "BTCUSDT"}
