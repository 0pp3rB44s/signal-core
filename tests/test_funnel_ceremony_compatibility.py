"""The owner ceremony's strategy-admission gate (lsrl.adapters.authority.admit_strategy) is a structural
check against ANY StrategyPort - strategy_id/spec_digest/symbol - with no reference to LSRL_V1 by name.
This proves FunnelStrategyPort is admitted/refused exactly like LsrlStrategyPort would be, WITHOUT running
the real ceremony or arming anything: MICRO_LIVE is out of scope for this restoration (see funnel/runtime.py),
so the identity below is registered as "consumed" the same test-only way test_lsrl_capital_authority.py's
own `consumed()` helper does (never a real ceremony run, never a real permit). This only proves the gate
itself does not need to change to support a second strategy, which is what "make the ceremony fully
functional end-to-end" requires before any real-capital authorization question is even asked.
"""
from __future__ import annotations

from lsrl.adapters.authority import AuthorityRefused, LiveAuthority
from lsrl.adapters.authority import admit_strategy
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import SymbolRules
from lsrl.tools import owner_ceremony as c
from app.config import Settings

from funnel.adapters.strategy_port import FunnelStrategyPort
from funnel.core.config import FunnelConfig
from funnel.core.engine import FunnelEngine


def _funnel_port(symbol: str = "SOLUSDT") -> FunnelStrategyPort:
    rules = SymbolRules(symbol=symbol, tick_size=0.001, qty_step=0.01, min_qty=0.01, min_notional_usdt=5.0)
    eng = FunnelEngine(FunnelConfig(symbol=symbol), rules, risk_gate=NullRiskGate(), settings=Settings())
    return FunnelStrategyPort(eng)


def _consumed(port: FunnelStrategyPort, *, strategy=None, config_hash=None, symbol=None) -> c.Identity:
    """An identity the ceremony "really returned", registered the same test-only way
    tests/test_lsrl_capital_authority.py's own consumed() helper does - never a real ceremony run."""
    ident = c.Identity(sha="a" * 40, config_hash=config_hash or port.spec_digest, runtime_config_hash="b" * 64,
                      account="FAKE_ACCOUNT", nav_usdt=100.0, capital_ceiling_usdt=100.0, exposure_ceiling_usdt=500.0,
                      strategy=strategy or port.strategy_id.upper(), symbol=symbol or port.symbol.upper())
    c._consumed[id(ident)] = ident
    return ident


def _authority_for(port: FunnelStrategyPort, **kw) -> LiveAuthority:
    class Gate:
        def blocked_reason(self):
            return None
    return LiveAuthority.trade(_consumed(port, **kw), capacity_gate=Gate())


def test_admit_strategy_accepts_the_funnel_port_when_identity_matches_exactly():
    port = _funnel_port()
    authority = _authority_for(port)
    admit_strategy(port, authority)          # must not raise


def test_admit_strategy_refuses_the_funnel_port_on_any_mismatch():
    port = _funnel_port()
    for kw in ({"strategy": "SOME_OTHER_STRATEGY"}, {"config_hash": "deadbeef"}, {"symbol": "BTCUSDT"}):
        authority = _authority_for(port, **kw)
        try:
            admit_strategy(port, authority)
            assert False, f"expected AuthorityRefused for {kw}"
        except AuthorityRefused as exc:
            assert "STRATEGY_NOT_APPROVED" in str(exc)


def test_two_different_symbols_are_two_different_strategy_ports_never_confused():
    sol = _funnel_port("SOLUSDT")
    btc = _funnel_port("BTCUSDT")
    assert sol.symbol != btc.symbol
    authority_for_sol = _authority_for(sol)
    try:
        admit_strategy(btc, authority_for_sol)
        assert False, "a BTCUSDT port must not be admitted under a SOLUSDT-approved identity"
    except AuthorityRefused:
        pass
