"""Bounded micro-live capital and the ONE authority object that lets a MICRO_LIVE executor open a position.

The owner chooses the capital inside 100-150 USDT; nothing here chooses it, raises a size to reach an exchange minimum, or lets a restart trade
without the ceremony. The executor is judged by what the (fake) EXCHANGE holds.
"""
from __future__ import annotations

import dataclasses as d
import json
import time
from types import SimpleNamespace

import pytest

from lsrl.adapters.authority import (PROTECT_ONLY, TRADE, AuthorityRefused, LiveAuthority, _authority_for_tests, bind_authority_to_runtime,
                                     runtime_config_digest)
from lsrl.adapters.capacity_gate import MonitorCapacityGate
from lsrl.adapters.live import LiveNotArmed, LSRLLiveExecutor
from lsrl.capital import (CAPITAL_MAX_USDT, CAPITAL_MIN_USDT, EXPOSURE_TO_CAPITAL_MAX, RISK_FRACTION, CapitalPolicy, CapitalRefused,
                          sizing_nav)
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.types import Direction, SetStop
from lsrl.tools import owner_ceremony as c
from tests.lsrl_fixtures import RULES, long_sweep
from tests.lsrl_fake_exchange import TEST_PROTECTION
from tests.test_lsrl_executor_hardening import entries, kinds
from tests.test_lsrl_exposure_recovery import LifeRig, converge, frozen_stop, verdict
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)


# ==================================================================================== the band (pure)
@pytest.mark.parametrize("capital,ok", [(99.99, False), (100, True), (100.0, True), (125.5, True), (150, True), (150.01, False), (0, False),
                                        (-100, False), (float("nan"), False), (float("inf"), False), (True, False), ("125", False), (None, False)])
def test_the_capital_ceiling_is_owner_selectable_only_inside_the_band(capital, ok):
    if ok:
        assert CapitalPolicy(capital, 100).capital_ceiling_usdt == capital
    else:
        with pytest.raises(CapitalRefused):
            CapitalPolicy(capital, 100)
    assert (CAPITAL_MIN_USDT, CAPITAL_MAX_USDT) == (100.0, 150.0)


@pytest.mark.parametrize("exposure,ok", [(1, True), (750, True), (750.01, False), (0, False), (-1, False), (float("nan"), False), (1e9, False), (True, False)])
def test_the_exposure_ceiling_can_be_lower_than_the_repository_leverage_never_higher(exposure, ok):
    if ok:
        CapitalPolicy(150, exposure)
    else:
        with pytest.raises(CapitalRefused):
            CapitalPolicy(150, exposure)
    assert EXPOSURE_TO_CAPITAL_MAX == 5.0 and RISK_FRACTION == 0.005


def test_the_micro_live_capital_is_never_chosen_by_the_code():
    """No default, no constant capital, no environment lookup: the band has two ends and nothing that selects a point in it."""
    import inspect
    import lsrl.capital as cap
    text = inspect.getsource(cap)
    assert "environ" not in text and "getenv" not in text and "DEFAULT_CAPITAL" not in text
    assert not [n for n in dir(cap) if "DEFAULT" in n.upper() or "RECOMMEND" in n.upper()]
    with pytest.raises(TypeError):
        CapitalPolicy()                                                         # no default amount exists


def test_the_strategy_sizes_against_the_smaller_of_equity_and_ceiling_never_more():
    assert sizing_nav(1_000_000, 125) == 125          # a huge account never enlarges a trade
    assert sizing_nav(80, 125) == 80                  # a smaller account shrinks it
    for bad in (0, -5, float("nan"), None, True):
        with pytest.raises(CapitalRefused):
            sizing_nav(bad, 125)


# ==================================================================================== the authority
def consumed(capital=150.0, exposure=750.0, nav=None):
    """An identity that `Permit.consume` really returned (the ceremony itself is exercised in test_lsrl_owner_ceremony.py)."""
    ident = c.Identity(c.BASE_SHA, c.CONFIG_HASH, "b" * 64, "FAKE_ACCOUNT", nav or capital, capital, exposure)
    c._consumed[id(ident)] = ident
    return ident


class Gate:
    def __init__(self, reason=None):
        self.reason = reason

    def blocked_reason(self):
        return self.reason


def test_a_trade_authority_exists_only_for_an_identity_the_ceremony_consumed():
    fake = c.Identity(c.BASE_SHA, c.CONFIG_HASH, "b" * 64, "FAKE_ACCOUNT", 150, 150, 750)
    with pytest.raises(AuthorityRefused, match="CEREMONY_NOT_CONSUMED"):
        LiveAuthority.trade(fake, capacity_gate=Gate())
    with pytest.raises(AuthorityRefused, match="CEREMONY_NOT_CONSUMED"):
        LiveAuthority(TRADE, fake, CapitalPolicy(150, 750), Gate())               # the direct constructor is no way around it
    real = consumed()
    with pytest.raises(AuthorityRefused, match="CEREMONY_NOT_CONSUMED"):
        LiveAuthority.trade(d.replace(real), capacity_gate=Gate())                # an equal COPY was never consumed
    ok = LiveAuthority.trade(real, capacity_gate=Gate())
    assert ok.mode == TRADE and ok.capital.capital_ceiling_usdt == 150.0
    with pytest.raises(AuthorityRefused, match="CAPITAL_OR_CAPACITY_GATE_MISSING"):
        LiveAuthority(TRADE, real, CapitalPolicy(150, 750), None)                 # no capacity gate, no trading
    with pytest.raises(AuthorityRefused, match="CAPITAL_OR_CAPACITY_GATE_MISSING"):
        LiveAuthority(TRADE, real, None, Gate())


def test_an_out_of_band_identity_cannot_become_a_trade_authority_even_if_consumed():
    for capital, exposure in ((99, 100), (151, 100), (150, 751)):
        ident = c.Identity(c.BASE_SHA, c.CONFIG_HASH, "b" * 64, "FAKE_ACCOUNT", 100, capital, exposure)
        c._consumed[id(ident)] = ident
        with pytest.raises(AuthorityRefused, match="CAPITAL_POLICY"):
            LiveAuthority.trade(ident, capacity_gate=Gate())


def test_protect_only_carries_no_trading_authority_and_the_unknown_mode_does_not_exist():
    p = LiveAuthority.protect_only()
    assert p.mode == PROTECT_ONLY and p.identity is None and p.capital is None
    assert p.entries_blocked() == "ENTRIES_DISABLED_PROTECT_ONLY"
    for kw in ({"identity": object()}, {"capital": CapitalPolicy(100, 100)}, {"unbounded": True}):
        with pytest.raises(AuthorityRefused):
            LiveAuthority(PROTECT_ONLY, **kw)
    with pytest.raises(AuthorityRefused, match="UNKNOWN_MODE"):
        LiveAuthority("TRADE_ANYWAY")


def test_the_executor_cannot_be_constructed_without_an_authority(tmp_path):
    rig = LifeRig(tmp_path)
    for bad in (None, "TRADE", object(), SimpleNamespace(mode="TRADE")):
        with pytest.raises(LiveNotArmed, match="CEREMONY_NOT_CONSUMED"):
            LSRLLiveExecutor(settings=rig.settings, client=rig.ex, submitter=rig.submitter, auth_store=rig.auth, gate=rig.gate,
                             state_store=rig.state, rules=RULES, process_down=TEST_PROTECTION, authority=bad)


# ==================================================================================== the executor under the REAL capital bounds
def band_rig(tmp_path, *, capital=150.0, exposure=750.0, nav=150.0, gate=None, protect_only=False):
    auth = LiveAuthority.protect_only() if protect_only else LiveAuthority.trade(consumed(capital, exposure), capacity_gate=gate or Gate())
    rig_cls = type("BandRig", (LifeRig,), {"authority": auth})
    rig = rig_cls(tmp_path)
    rig.auth_obj = auth
    return rig.run(long_sweep(nav=nav))


def resting(rig):
    return [o for o in rig.ex.orders.values() if o["state"] in ("live", "partially_filled")]


def resting_entries(rig):
    return [o for o in resting(rig) if o["tradeSide"] == "open"]


def test_a_ladder_inside_the_band_is_placed_exactly_as_the_frozen_strategy_sized_it(tmp_path):
    rig = band_rig(tmp_path)
    assert len(entries(rig)) == 5 and rig.exec.halt_reason is None
    planned = sum(float(o["size"]) * abs(float(o["price"]) - frozen_stop(rig)) for o in resting(rig))
    assert planned <= 150.0 * RISK_FRACTION * (1 + 1e-9)                          # the frozen 0.5 % fraction, never more
    assert "CAPITAL_GUARD_VETO" not in kinds(rig)


def test_a_ladder_above_the_owners_exposure_ceiling_is_refused_whole_and_nothing_is_sent(tmp_path):
    rig = band_rig(tmp_path, exposure=100.0)
    assert resting(rig) == [] and rig.ex.orders == {}
    assert "CAPITAL_GUARD_VETO" in kinds(rig)
    assert any("EXPOSURE_ABOVE_CEILING" in str(i) for i in rig.exec.incidents)
    assert rig.exec.halt_reason is None                                            # a veto is a clean rejection, not a fault
    assert rig.ex.position == {"long": 0.0, "short": 0.0} and rig.ex.plans == []


def test_a_strategy_sized_on_a_larger_nav_than_the_ceiling_is_vetoed_by_the_executor_itself(tmp_path):
    """The runtime sizes against min(equity, ceiling), but a misconfigured feed must not be able to enlarge a trade: the executor bounds risk
    by the ceiling on its own."""
    rig = band_rig(tmp_path, nav=1000.0)
    assert resting(rig) == [] and rig.ex.orders == {}
    assert any("RISK_ABOVE_FROZEN_FRACTION" in str(i) for i in rig.exec.incidents)


def test_the_veto_never_resizes_a_rung_it_only_refuses_the_ladder(tmp_path):
    rig = band_rig(tmp_path, nav=1000.0)
    assert not [1 for o in rig.ex.orders.values()]                                # no thinned, shrunk or partial ladder
    rig2 = band_rig(tmp_path / "ok", nav=150.0)
    sizes = {float(o["size"]) for o in resting(rig2)}
    assert len(sizes) == 1                                                        # all rungs equal: exactly the frozen split, untouched


def test_an_exchange_minimum_is_never_reached_by_upsizing(tmp_path):
    """At a capital whose risk budget cannot buy the minimum quantity the frozen sizing says NO-TRADE; the executor never sees a bigger order."""
    rig = band_rig(tmp_path, capital=100.0, exposure=500.0, nav=100.0)
    for o in rig.ex.orders.values():
        assert float(o["size"]) >= RULES.min_qty and float(o["size"]) * float(o["price"]) >= RULES.min_notional_usdt
    # NAV 1 (far below the floor): nothing tradeable, nothing sent
    tiny = band_rig(tmp_path / "tiny", nav=1.0)
    assert tiny.ex.orders == {}


def test_a_dead_or_alarming_capacity_monitor_blocks_new_ladders_but_never_protection(tmp_path):
    gate = Gate("MONITOR_STALE:900s")
    rig = band_rig(tmp_path, gate=gate)
    assert rig.ex.orders == {} and any("CAPACITY_GATE:MONITOR_STALE" in str(i) for i in rig.exec.incidents)
    # a position that exists is still protected while the gate is closed
    ok = band_rig(tmp_path / "ok", gate=Gate())
    ok.ex.fill(entries(ok)[0])
    ok.auth_obj.capacity_gate.reason = "ACTIVE_ALARMS:DRAIN"
    ok.drive(5)
    assert verdict(ok) == "PROTECTED" and ok.exec.halt_reason is None


def test_protect_only_never_places_an_entry_yet_the_engine_is_unaffected(tmp_path):
    rig = band_rig(tmp_path, protect_only=True)
    assert rig.ex.orders == {} and any("ENTRIES_DISABLED_PROTECT_ONLY" in str(i) for i in rig.exec.incidents)
    assert rig.exec.halt_reason is None


def test_a_restart_without_the_ceremony_protects_what_exists_and_cancels_every_resting_entry(tmp_path):
    """The respawn scenario: a ladder is resting and one rung has filled when the process dies. The respawn has no permit, so it can only PROTECT:
    the filled rung keeps its stop, the rest of the ladder is cancelled, and nothing new is ever placed."""
    rig = band_rig(tmp_path)
    first = entries(rig)[0]
    rig.ex.fill(first)
    rig.drive(5)
    assert verdict(rig) == "PROTECTED" and len(resting_entries(rig)) == 4
    n_orders = len(rig.ex.orders)
    rig.authority = LiveAuthority.protect_only()
    rig.restart()                                                                   # a new process: NO consumed permit
    rig.drive(30)
    assert resting_entries(rig) == [], "a previous life's ladder must not stay armed"
    converge(rig)
    assert verdict(rig) == "PROTECTED" and rig.exec.emergency is None
    assert len(rig.ex.orders) <= n_orders + 1                                       # (a re-placed take-profit at most) - no new entry
    assert not [o for o in rig.ex.orders.values() if o["tradeSide"] == "open" and o["clientOid"] not in
                {k for k in rig.ex.orders if k in rig.exec.orders}]
    assert rig.exec.authority.mode == PROTECT_ONLY


# ==================================================================================== the monitor-backed capacity gate
def write_monitor(tmp_path, *, t, alive=True, interval=60.0, active=None):
    (tmp_path / "monitor_status.json").write_text(json.dumps({"t": t, "alive": alive, "interval_s": interval}))
    (tmp_path / "ALARM.json").write_text(json.dumps({"t": t, "active": active or {}}))


def test_the_capacity_gate_opens_only_for_a_fresh_quiet_monitor(tmp_path):
    now = 1_800_000_000.0
    g = MonitorCapacityGate(tmp_path, now=lambda: now)
    assert g.blocked_reason() == "MONITOR_STATUS_UNAVAILABLE"                      # nothing there: closed
    write_monitor(tmp_path, t=now - 30)
    assert g.blocked_reason() is None
    write_monitor(tmp_path, t=now - 30, active={"FREE_LOW": {"since": now}, "DRAIN": {}})
    assert g.blocked_reason() == "ACTIVE_ALARMS:DRAIN,FREE_LOW"
    write_monitor(tmp_path, t=now - 900)
    assert g.blocked_reason().startswith("MONITOR_STALE")
    write_monitor(tmp_path, t=now + 600)
    assert g.blocked_reason().startswith("MONITOR_STALE")                          # a heartbeat from the future is not fresh
    write_monitor(tmp_path, t=now - 30, alive=False)
    assert g.blocked_reason() == "MONITOR_STATUS_UNAVAILABLE"
    (tmp_path / "monitor_status.json").write_text("{not json")
    assert g.blocked_reason() == "MONITOR_STATUS_UNAVAILABLE"
    write_monitor(tmp_path, t=now - 30)
    (tmp_path / "ALARM.json").write_text("[]")
    assert g.blocked_reason() == "ALARM_FILE_UNREADABLE"
    (tmp_path / "monitor_status.json").write_text(json.dumps({"t": "x", "alive": True, "interval_s": 60}))
    assert g.blocked_reason() == "MONITOR_STATUS_MALFORMED"


# ==================================================================================== the runtime is exactly what the owner approved
def settings(**over):
    base = dict(lsrl_mode="MICRO_LIVE", lsrl_symbol="SOLUSDT", lsrl_microlive_armed=True, execution_enabled=True, execution_mode="LIVE",
                forward_paper_only=False, bitget_product_type="USDT-FUTURES", default_leverage=5.0, max_leverage=5.0,
                max_total_exposure_pct=200.0, max_cluster_exposure_pct=120.0, execution_max_live_notional_per_trade_usdt=0.0,
                lsrl_min_free_gb=1.0, lsrl_process_down_attestation_sha256="a" * 64, lsrl_accept_demo_process_down_evidence=True)
    base.update(over)
    return SimpleNamespace(**base)


def approved_authority(s, capital=150.0, exposure=750.0, sha=c.BASE_SHA):
    digest = runtime_config_digest(s, CFG, capital_ceiling_usdt=capital, exposure_ceiling_usdt=exposure)
    ident = c.Identity(sha, c.CONFIG_HASH, digest, "FAKE_ACCOUNT", capital, capital, exposure)
    c._consumed[id(ident)] = ident
    return LiveAuthority.trade(ident, capacity_gate=Gate())


def test_the_running_configuration_must_equal_the_approved_one_field_for_field():
    s = settings()
    auth = approved_authority(s)
    bind_authority_to_runtime(auth, settings=s, cfg=CFG, git_sha=c.BASE_SHA, symbol="SOLUSDT")                 # exact match: fine
    for field, value in (("max_leverage", 8.0), ("default_leverage", 8.0), ("max_total_exposure_pct", 400.0), ("execution_max_live_notional_per_trade_usdt", 5000.0),
                         ("lsrl_process_down_attestation_sha256", "b" * 64), ("lsrl_accept_demo_process_down_evidence", False),
                         ("lsrl_min_free_gb", 0.1), ("execution_mode", "PAPER")):
        with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:runtime_config_hash"):
            bind_authority_to_runtime(auth, settings=settings(**{field: value}), cfg=CFG, git_sha=c.BASE_SHA, symbol="SOLUSDT")


def test_another_release_symbol_or_strategy_config_is_refused():
    s = settings()
    auth = approved_authority(s)
    with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:sha"):
        bind_authority_to_runtime(auth, settings=s, cfg=CFG, git_sha="c" * 40, symbol="SOLUSDT")
    with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:sha"):
        bind_authority_to_runtime(auth, settings=s, cfg=CFG, git_sha=c.BASE_SHA + "+dirty", symbol="SOLUSDT")
    with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:symbol"):
        bind_authority_to_runtime(auth, settings=s, cfg=CFG, git_sha=c.BASE_SHA, symbol="BTCUSDT")
    other = d.replace(CFG, sl_atr1m_mult=0.30)
    with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:config_hash"):
        bind_authority_to_runtime(auth, settings=s, cfg=other, git_sha=c.BASE_SHA, symbol="SOLUSDT")


def test_the_capital_is_part_of_the_approved_runtime_configuration():
    s = settings()
    digest_150 = runtime_config_digest(s, CFG, capital_ceiling_usdt=150, exposure_ceiling_usdt=750)
    assert digest_150 != runtime_config_digest(s, CFG, capital_ceiling_usdt=100, exposure_ceiling_usdt=750)
    assert digest_150 != runtime_config_digest(s, CFG, capital_ceiling_usdt=150, exposure_ceiling_usdt=500)


def test_protect_only_binds_to_nothing_and_the_test_authority_never_reaches_a_real_binding():
    bind_authority_to_runtime(LiveAuthority.protect_only(), settings=settings(), cfg=CFG, git_sha="x", symbol="Y")
    assert _authority_for_tests().unbounded is True


def test_place_entry_itself_refuses_when_entries_are_blocked_even_if_the_cycle_gate_were_skipped(tmp_path):
    """Defence in depth: a duplicate delivery of a claimed setup skips the cycle gate's later checks, never the authority's."""
    from lsrl.core.types import PlaceEntry
    rig = band_rig(tmp_path, gate=Gate("ACTIVE_ALARMS:FREE_LOW"))
    events = rig.exec._place_entry(PlaceEntry("x", "x-e1", 1, Direction.LONG, 149.5, 0.2, 1), rig.ex.now_ms)
    assert events and events[0].status == "REJECTED" and events[0].reason.startswith("CAPACITY_GATE:ACTIVE_ALARMS") and rig.ex.orders == {}


def test_promotion_happens_once_from_protect_only_and_only_to_a_trade_authority(tmp_path):
    rig = band_rig(tmp_path, protect_only=True)
    trade = LiveAuthority.trade(consumed(), capacity_gate=Gate())
    with pytest.raises(LiveNotArmed, match="TRADE authority is required"):
        rig.exec.promote(LiveAuthority.protect_only())                             # PROTECT_ONLY is not a promotion
    for bad in (None, "TRADE", object()):
        with pytest.raises(LiveNotArmed, match="TRADE authority is required"):
            rig.exec.promote(bad)
    rig.exec.promote(trade)
    assert rig.exec.authority is trade and any(i["kind"] == "PROMOTED_TO_TRADE" for i in rig.exec.incidents)
    with pytest.raises(LiveNotArmed, match="already trading"):
        rig.exec.promote(LiveAuthority.trade(consumed(), capacity_gate=Gate()))    # a TRADE executor is never re-promoted
    assert rig.exec.summary()["authority"] == TRADE


# ==================================================================================== the real runtime builder: PROTECT_ONLY first, TRADE only by promotion
def build_live_runtime(tmp_path, monkeypatch, authority):
    from lsrl.runtime import build_runtime
    from tests.test_lsrl_feed_and_runtime import MetaClient, settings as base_settings, spec
    import tests.test_lsrl_process_down_protection as pdp
    monkeypatch.setattr("lsrl.runtime.resolve_git_sha", lambda *a, **k: pdp.SHA)
    path, digest = pdp.attestation(tmp_path)
    monkeypatch.setattr("lsrl.adapters.process_down.time", SimpleNamespace(time=lambda: pdp.NOW))

    class AccountClient(MetaClient):
        equity = 5_000.0

        def get_accounts(self, product_type=None):
            return {"data": [{"marginCoin": "USDT", "accountEquity": str(self.equity)}]}
    st = base_settings(lsrl_mode="MICRO_LIVE", lsrl_data_dir=str(tmp_path / "d"), forward_paper_only=False, execution_enabled=True, execution_mode="LIVE",
                       lsrl_microlive_armed=True, lsrl_process_down_attestation_path=str(path), lsrl_process_down_attestation_sha256=digest,
                       lsrl_accept_demo_process_down_evidence=True, default_leverage=5.0, max_leverage=5.0, max_total_exposure_pct=200.0,
                       max_cluster_exposure_pct=120.0, execution_max_live_notional_per_trade_usdt=0.0, execution_enabled_extra=None)
    client = AccountClient(spec())
    return st, client, build_runtime(st, client=client, execution_service=object(), risk_manager=None, authority=authority)


def test_the_real_builder_starts_protect_only_and_promotion_swaps_in_the_capped_nav_and_the_trade_authority(tmp_path, monkeypatch):
    import tests.test_lsrl_process_down_protection as pdp
    st, client, rt = build_live_runtime(tmp_path, monkeypatch, LiveAuthority.protect_only())
    assert rt.mode.value == "MICRO_LIVE" and rt.driver.executor.authority.mode == PROTECT_ONLY
    assert rt.driver.nav_fn() == 5_000.0                                            # no capital yet: the raw equity (nothing can trade on it)
    assert rt.equity_fn() == 5_000.0                                                # what the ceremony's NAV observation reads
    assert rt.driver.executor.summary()["recovered"] is False and rt.driver.executor.summary()["open_setups"] == 0
    ok = approved_authority(st, capital=125.0, exposure=500.0, sha=pdp.SHA)
    rt.promote(ok)
    assert rt.driver.executor.authority is ok and rt.driver.executor.authority.mode == TRADE
    assert rt.driver.nav_fn() == 125.0                                              # min(5,000 equity, 125 ceiling)
    client.equity = 90.0
    assert rt.driver.nav_fn() == 90.0
    with pytest.raises(LiveNotArmed):
        rt.promote(approved_authority(st, sha=pdp.SHA))                             # already trading
    assert rt.status()["executor"]["authority"] == TRADE


def test_a_promotion_bound_to_another_configuration_or_release_is_refused_and_the_runtime_stays_protect_only(tmp_path, monkeypatch):
    import tests.test_lsrl_process_down_protection as pdp
    st, client, rt = build_live_runtime(tmp_path, monkeypatch, LiveAuthority.protect_only())
    other = settings(max_leverage=8.0)                                              # approved under a DIFFERENT effective configuration
    with pytest.raises(AuthorityRefused, match="IDENTITY_MISMATCH:runtime_config_hash"):
        rt.promote(approved_authority(other, sha=pdp.SHA))
    assert rt.driver.executor.authority.mode == PROTECT_ONLY and rt.driver.nav_fn() == 5_000.0
    toy_identity = c.Identity(pdp.SHA, c.CONFIG_HASH, runtime_config_digest(st, CFG, capital_ceiling_usdt=125, exposure_ceiling_usdt=500), "FAKE_ACCOUNT",
                              125, 125, 500, strategy="OTHER_STRATEGY")
    c._consumed[id(toy_identity)] = toy_identity
    with pytest.raises(AuthorityRefused, match="STRATEGY_NOT_APPROVED:strategy"):
        rt.promote(LiveAuthority.trade(toy_identity, capacity_gate=Gate()))
    assert rt.driver.executor.authority.mode == PROTECT_ONLY
