"""The production ceremony integration: release policy -> provider (19 gates) -> ceremony -> promotion, and the restart semantics.

Everything that needs an exchange is a fake; what is real is the wiring, the refusal order, the terminal handling and the in-process promotion. The alpha gate
is an artifact that does not exist today: the tests show that with it missing NOTHING the owner types can arm live, and that the SAME code path arms only when
every gate, including that artifact, is genuinely present.
"""
from __future__ import annotations

import dataclasses as d
import hashlib
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lsrl.adapters.authority import PROTECT_ONLY, TRADE, LiveAuthority
from lsrl.adapters.capacity_gate import MonitorCapacityGate
from lsrl.capital import CapitalPolicy
from lsrl.core.config import DEFAULT_CONFIG
from lsrl.tools import gate_collectors as gc
from lsrl.tools import live_launcher as ll
from lsrl.tools import owner_ceremony as oc
from lsrl.tools.ceremony_provider import Collectors, ProductionProvider, canonical_digest
from lsrl.tools.gate_collectors import Observation
from lsrl.tools.release_policy import ARTIFACT_GATES, PolicyRefused, load_policy
from tests.test_lsrl_process_down_protection import FROZEN_REQUIRED_CHECKS

ROOT = Path(__file__).resolve().parents[1]
SHA = oc.BASE_SHA
RUNTIME_HASH = "b" * 64
CLEAN_AUDIT = {"complete": True, "REAL_POSITIONS": 0, "REAL_ORDERS": 0, "REAL_PLAN_ORDERS": 0, "EXCHANGE_EXPOSURE_GATE": "PASS",
               "scope": {"product_types": ["USDT-FUTURES", "USDC-FUTURES"]}, "positions": [], "orders": [], "plan_orders": []}


def write_json(path: Path, doc) -> str:
    raw = json.dumps(doc, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def artifact_docs(now):
    exp = now + 3600
    attestation_checks = [{"id": c, "passed": True, "observed": {}} for c in FROZEN_REQUIRED_CHECKS]
    return {
        "prospective_green": {"kind": "LSRL_PROSPECTIVE_GREEN", "protocol_id": "LSRL_PROSPECTIVE_PROTOCOL_V1", "git_sha": SHA,
                              "config_digest": oc.CONFIG_HASH, "reproduced_by": "AGENT_C", "decision": "PASS", "expires_at": exp},
        "engineering_approval": {"kind": "LSRL_ENGINEERING_APPROVAL", "git_sha": SHA, "reviewer": "AGENT_C", "decision": "PASS", "expires_at": exp},
        "process_down_protection": {"kind": "PRESET_STOP_SEMANTICS", "schema": 2, "decision": "PASS", "environment": "BITGET_DEMO", "code_sha": SHA,
                                    "created_at": now - 60, "expires_at": exp, "observed_model": "PLAN_LEVEL", "sides_tested": ["long", "short"],
                                    "capabilities": {"plan_place_first_ok": False}, "checks": attestation_checks,
                                    "evidence": {"run_id": "r1", "orders": [{"orderId": "1", "clientOid": "lsrlprobe-r1-l-fill1"}]}},
        "exchange_semantics": {"kind": "LSRL_EXCHANGE_SEMANTICS_REPRODUCTION", "git_sha": SHA, "reproduced_by": "AGENT_C", "read_only": True,
                               "decision": "PASS", "expires_at": exp},
    }


class World:
    """A release policy and its four pinned artifacts on disk. `drop` leaves artifacts out; `edit` changes an artifact AFTER it was pinned or before."""

    def __init__(self, tmp: Path, *, capital=125.0, exposure=500.0, drop=(), amend=None, repin=True, account="FAKE_ACCOUNT"):
        now = time.time()
        docs = artifact_docs(now)
        for name, fn in (amend or {}).items():
            if name != "exchange_semantics":
                fn(docs[name])
        self.paths, refs = {}, {}
        for name in ("process_down_protection", "prospective_green", "engineering_approval", "exchange_semantics"):
            path = tmp / f"{name}.json"
            if name == "exchange_semantics":                               # C's reproduction names the attestation it reproduced
                docs[name]["attestation_sha256"] = refs["process_down_protection"]["sha256"]
                for fn in [(amend or {}).get(name)]:
                    if fn:
                        fn(docs[name])
            digest = write_json(path, docs[name])
            self.paths[name] = path
            refs[name] = {"path": str(path), "sha256": digest}
            if name in drop:
                path.unlink()
        policy = {"kind": "LSRL_MICROLIVE_RELEASE_POLICY", "schema": 1, "decision": "APPROVE", "created_at": now - 60, "expires_at": now + 3600,
                  "approved": {"sha": SHA, "config_hash": oc.CONFIG_HASH, "runtime_config_hash": RUNTIME_HASH, "account": account,
                               "capital_ceiling_usdt": capital, "exposure_ceiling_usdt": exposure}, "artifacts": refs}
        self.policy_path = tmp / "policy.json"
        self.digest = write_json(self.policy_path, policy)
        self.policy = load_policy(self.policy_path, self.digest)


def collectors(**over) -> Collectors:
    ok = lambda **doc: (lambda: Observation(True, doc or {"ok": True}))       # noqa: E731
    base = dict(git=lambda: {"sha": SHA, "clean": True, "strategy_freeze": True}, equity=lambda: 1000.0, runtime_config_hash=lambda p: RUNTIME_HASH,
                accept_demo=lambda: True, audit=lambda: dict(CLEAN_AUDIT), clock=ok(clock="safe"), feed=ok(feed="OK"), recorder=ok(rec="ok"),
                disk=ok(disk="ok"), risk_manager=ok(rm="central"), kill_switches=ok(ks="armed"), reconciliation=ok(rec="flat"))
    base.update(over)
    return Collectors(**base)


def provider(world: World, **over) -> ProductionProvider:
    return ProductionProvider(world.policy, collectors(**over))


# ==================================================================================== the release policy
def test_a_well_formed_policy_loads_with_the_owners_capital_untouched(tmp_path):
    w = World(tmp_path, capital=137.5, exposure=400.0)
    assert w.policy.capital == CapitalPolicy(137.5, 400.0) and w.policy.account == "FAKE_ACCOUNT" and set(w.policy.artifacts) == set(ARTIFACT_GATES)


@pytest.mark.parametrize("mutate,reason", [
    (lambda d_: d_.update(kind="OTHER"), "KIND_OR_SCHEMA"), (lambda d_: d_.update(schema=2), "KIND_OR_SCHEMA"),
    (lambda d_: d_.update(decision="DRAFT"), "NOT_APPROVED"), (lambda d_: d_.update(expires_at=1.0), "EXPIRED_OR_UNBOUNDED"),
    (lambda d_: d_.update(expires_at=time.time() + 30 * 86400), "EXPIRED_OR_UNBOUNDED"),
    (lambda d_: d_.update(created_at=time.time() + 3600, expires_at=time.time() + 7200), "EXPIRED_OR_UNBOUNDED"),
    (lambda d_: d_.update(created_at="x"), "EXPIRED_OR_UNBOUNDED"),
    (lambda d_: d_["approved"].update(sha="nothex"), "IDENTITY_MALFORMED"), (lambda d_: d_["approved"].update(config_hash="x" * 64), "IDENTITY_MALFORMED"),
    (lambda d_: d_["approved"].update(runtime_config_hash="short"), "IDENTITY_MALFORMED"),
    (lambda d_: d_["approved"].update(capital_ceiling_usdt=99), "POLICY_CAPITAL"), (lambda d_: d_["approved"].update(capital_ceiling_usdt=151), "POLICY_CAPITAL"),
    (lambda d_: d_["approved"].update(capital_ceiling_usdt=None), "POLICY_CAPITAL"), (lambda d_: d_["approved"].update(exposure_ceiling_usdt=1e9), "POLICY_CAPITAL"),
    (lambda d_: d_["approved"].pop("account"), "APPROVED_FIELDS"), (lambda d_: d_["approved"].update(extra=1), "APPROVED_FIELDS"),
    (lambda d_: d_["artifacts"].pop("prospective_green"), "ARTIFACTS_INCOMPLETE"), (lambda d_: d_["artifacts"].update(extra={}), "ARTIFACTS_INCOMPLETE"),
    (lambda d_: d_["artifacts"]["engineering_approval"].update(sha256="short"), "ARTIFACT_REF:engineering_approval"),
    (lambda d_: d_["artifacts"]["exchange_semantics"].update(path=""), "ARTIFACT_REF:exchange_semantics"),
])
def test_every_way_a_policy_can_be_wrong_refuses(tmp_path, mutate, reason):
    w = World(tmp_path)
    doc = json.loads(w.policy_path.read_text())
    mutate(doc)
    digest = write_json(w.policy_path, doc)
    with pytest.raises(PolicyRefused, match=reason):
        load_policy(w.policy_path, digest)


def test_a_policy_cannot_vouch_for_itself_the_digest_must_be_the_one_the_owner_typed(tmp_path):
    w = World(tmp_path)
    for typed in ("", "abc", "g" * 64, "0" * 64, w.digest.upper()[:-1] + "0"):
        with pytest.raises(PolicyRefused):
            load_policy(w.policy_path, typed)
    w.policy_path.write_bytes(w.policy_path.read_bytes() + b" ")
    with pytest.raises(PolicyRefused, match="DIGEST_MISMATCH"):
        load_policy(w.policy_path, w.digest)
    link = tmp_path / "link.json"
    link.symlink_to(w.policy_path)
    with pytest.raises(PolicyRefused, match="NOT_PRESENT"):
        load_policy(link, w.digest)
    with pytest.raises(PolicyRefused, match="NOT_PRESENT"):
        load_policy(tmp_path / "nope.json", w.digest)


# ==================================================================================== the provider computes 19 gates and grants none
def test_with_every_gate_genuinely_present_the_snapshot_validates(tmp_path):
    w = World(tmp_path)
    snap = provider(w).snapshot()
    oc.validate(snap)
    assert set(snap.gates) == set(oc.GATES) and snap.identity == snap.approved_identity
    assert snap.identity.nav_usdt == 125.0 and snap.identity.capital_ceiling_usdt == 125.0        # min(1000 equity, the owner's ceiling)
    assert snap.approved_digests["owner_capital_approval"] == w.digest
    assert all(snap.approved_digests[n] == w.policy.artifacts[n].sha256 for n in ARTIFACT_GATES)


def test_the_nav_is_the_smaller_of_equity_and_the_owners_ceiling_and_below_the_floor_refuses(tmp_path):
    w = World(tmp_path, capital=150.0)
    assert provider(w, equity=lambda: 132.0).snapshot().identity.nav_usdt == 132.0
    with pytest.raises(oc.Refused, match="NAV_BELOW_MICRO_LIVE_FLOOR"):
        oc.validate(provider(w, equity=lambda: 99.0).snapshot())
    with pytest.raises(oc.Refused, match="CAPITAL_UNKNOWN"):
        oc.validate(provider(w, equity=lambda: (_ for _ in ()).throw(TimeoutError("no equity"))).snapshot())      # unreadable = unknown


def _drop(name):
    return dict(drop=(name,))


GATE_BREAKERS = {
    "approved_sha": dict(git=lambda: {"sha": "c" * 40, "clean": True, "strategy_freeze": True}),
    "clean_tree": dict(git=lambda: {"sha": SHA, "clean": False, "strategy_freeze": True}),
    "approved_config": dict(runtime_config_hash=lambda p: "c" * 64),
    "strategy_freeze": dict(git=lambda: {"sha": SHA, "clean": True, "strategy_freeze": False}),
    "authenticated_exposure": dict(audit=lambda: {**CLEAN_AUDIT, "complete": False}),
    "no_unexpected_exposure": dict(audit=lambda: {**CLEAN_AUDIT, "REAL_POSITIONS": 1, "positions": [{"product_type": "USDT-FUTURES", "symbol": "BTCUSDT", "side": "long"}]}),
    "exchange_protection": dict(audit=lambda: {**CLEAN_AUDIT, "REAL_PLAN_ORDERS": 2}),
    "resting_orders_elsewhere": dict(audit=lambda: {**CLEAN_AUDIT, "REAL_ORDERS": 1, "orders": []}),        # the COUNT says 1, the rows say none: the count wins
    "clock": dict(clock=lambda: Observation(False, {"clock": "unsafe"})),
    "feed": dict(feed=lambda: Observation(False, {"feed": "UNSAFE"})),
    "recorder": dict(recorder=lambda: Observation(False, {"recorder_error": True})),
    "disk": dict(disk=lambda: Observation(False, {"free_gb_ok": False})),
    "risk_manager": dict(risk_manager=lambda: Observation(False, {"central_gate": False})),
    "kill_switches": dict(kill_switches=lambda: Observation(False, {"daily_report_readable": False})),
    "reconciliation": dict(reconciliation=lambda: Observation(False, {"recovered": False})),
}


@pytest.mark.parametrize("gate", sorted(GATE_BREAKERS))
def test_each_live_observation_failing_alone_refuses_the_ceremony(tmp_path, gate):
    w = World(tmp_path)
    with pytest.raises(oc.Refused):
        oc.validate(provider(w, **GATE_BREAKERS[gate]).snapshot())


BREAKER_FLIPS = {"approved_sha": "approved_sha", "clean_tree": "clean_tree", "approved_config": "approved_config", "strategy_freeze": "strategy_freeze",
                 "authenticated_exposure": "authenticated_exposure", "no_unexpected_exposure": "no_unexpected_exposure",
                 "exchange_protection": "exchange_protection", "resting_orders_elsewhere": "no_unexpected_exposure", "clock": "clock", "feed": "feed",
                 "recorder": "recorder", "disk": "disk", "risk_manager": "risk_manager", "kill_switches": "kill_switches", "reconciliation": "reconciliation"}


@pytest.mark.parametrize("breaker", sorted(BREAKER_FLIPS))
def test_each_breaker_flips_its_own_gate_not_merely_some_later_check(tmp_path, breaker):
    """The ceremony has redundant layers (identity equality, exposure rows). The GATE itself must still say False: no layer may stand in for another."""
    snap = provider(World(tmp_path), **GATE_BREAKERS[breaker]).snapshot()
    assert snap.gates[BREAKER_FLIPS[breaker]].passed is False, breaker
    others = {g for g, e in snap.gates.items() if e.passed is False} - {BREAKER_FLIPS[breaker]}
    assert "owner_capital_approval" not in others


def test_a_demo_attestation_is_only_accepted_when_the_owners_flag_says_so(tmp_path):
    w = World(tmp_path)
    assert provider(w, accept_demo=lambda: True).snapshot().gates["process_down_protection"].passed is True
    assert provider(w, accept_demo=lambda: False).snapshot().gates["process_down_protection"].passed is False
    with pytest.raises(oc.Refused, match="GATE_NOT_MET:process_down_protection"):
        oc.validate(provider(w, accept_demo=lambda: False).snapshot())


def test_an_unexpected_failure_anywhere_in_the_ceremony_is_a_refusal_never_a_crash_or_a_promotion(tmp_path, capsys):
    w = World(tmp_path)
    deps, runtime, calls = make_deps(w)
    deps.ceremony = lambda provider: (_ for _ in ()).throw(RuntimeError("boom " + "K" * 30))
    assert ll.launch(args_for(w), deps) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["reason"] == "UNEXPECTED:RuntimeError" and "K" * 30 not in json.dumps(out)
    assert runtime.stopped is True and runtime.promotions == []


@pytest.mark.parametrize("name,gate", [("prospective_green", "prospective_green"), ("engineering_approval", "engineering_approval"),
                                       ("process_down_protection", "process_down_protection"), ("exchange_semantics", "exchange_semantics")])
def test_each_pinned_artifact_missing_alone_refuses_naming_its_gate(tmp_path, name, gate):
    w = World(tmp_path, **_drop(name))
    with pytest.raises(oc.Refused, match=f"GATE_NOT_MET:{gate}"):
        oc.validate(provider(w).snapshot())


def test_the_slow_reads_run_first_so_the_live_health_evidence_is_the_freshest(tmp_path):
    w = World(tmp_path)
    log = []

    def slow_audit():
        log.append(("audit_start", time.monotonic()))
        time.sleep(0.25)
        log.append(("audit_end", time.monotonic()))
        return dict(CLEAN_AUDIT)

    def health(name):
        def fn():
            log.append((name, time.monotonic()))
            return Observation(True, {name: "ok"})
        return fn
    snap = provider(w, audit=slow_audit, clock=health("clock"), feed=health("feed"), recorder=health("recorder"), disk=health("disk"),
                    risk_manager=health("risk_manager"), kill_switches=health("kill_switches"), reconciliation=health("reconciliation")).snapshot()
    audit_end = next(t for n, t in log if n == "audit_end")
    starts = [t for n, t in log if n in ("clock", "feed", "recorder", "disk", "risk_manager", "kill_switches", "reconciliation")]
    assert len(starts) == 7 and all(t >= audit_end for t in starts)                       # every health observation started AFTER the audit finished
    assert snap.gates["feed"].observed_mono > snap.gates["no_unexpected_exposure"].observed_mono
    oc.validate(snap)


def test_a_collector_that_raises_is_a_failed_gate_never_a_passed_one(tmp_path):
    w = World(tmp_path)
    boom = lambda: (_ for _ in ()).throw(RuntimeError("secret " + "K" * 30))          # noqa: E731
    snap = provider(w, clock=boom, audit=boom, feed=boom).snapshot()
    assert snap.gates["clock"].passed is False and snap.gates["feed"].passed is False and snap.audit_complete is False
    with pytest.raises(oc.Refused):
        oc.validate(snap)
    assert "K" * 30 not in json.dumps([e.source for e in snap.gates.values()])


# ==================================================================================== the ALPHA gate is an artifact nobody can fake
ALPHA_FORGERIES = {
    "decision_fail": lambda a: a.update(decision="FAIL"),
    "expired": lambda a: a.update(expires_at=time.time() - 10),
    "another_release": lambda a: a.update(git_sha="c" * 40),
    "another_config": lambda a: a.update(config_digest="c" * 64),
    "another_protocol": lambda a: a.update(protocol_id="LSRL_PROSPECTIVE_PROTOCOL_V0"),
    "wrong_kind": lambda a: a.update(kind="LSRL_ENGINEERING_APPROVAL"),
    "self_attested_by_the_builder": lambda a: a.update(reproduced_by="AGENT_A"),
    "no_reproduction": lambda a: a.pop("reproduced_by"),
    "boolean_only": lambda a: (a.clear(), a.update(prospective_green=True, decision="PASS")),
}


@pytest.mark.parametrize("forgery", sorted(ALPHA_FORGERIES))
def test_a_forged_or_self_attested_alpha_artifact_never_satisfies_the_gate(tmp_path, forgery):
    w = World(tmp_path, amend={"prospective_green": ALPHA_FORGERIES[forgery]})
    with pytest.raises(oc.Refused, match="GATE_NOT_MET:prospective_green"):
        oc.validate(provider(w).snapshot())


def test_an_alpha_artifact_edited_after_the_owner_pinned_it_is_refused(tmp_path):
    w = World(tmp_path)
    doc = json.loads(w.paths["prospective_green"].read_text())
    doc["expires_at"] += 86400
    w.paths["prospective_green"].write_text(json.dumps(doc))                            # same path, different bytes: the pin no longer matches
    with pytest.raises(oc.Refused, match="GATE_NOT_MET:prospective_green"):
        oc.validate(provider(w).snapshot())


def _tree(module):
    import ast
    return ast.parse(Path(module.__file__).read_text())


def test_the_alpha_evidence_cannot_be_supplied_by_the_environment_a_flag_or_a_source_file():
    """AST scan of every module on the ceremony path: no environment lookup at all, and the only command-line flags are the five the launcher documents."""
    import ast
    import lsrl.tools.ceremony_provider as provider_mod
    import lsrl.tools.gate_collectors as collectors_mod
    import lsrl.tools.release_policy as policy_mod
    for module in (ll, oc, provider_mod, collectors_mod, policy_mod):
        for node in ast.walk(_tree(module)):
            assert not (isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb")), (module.__name__, node.lineno)
    flags = set()
    for node in ast.walk(_tree(ll)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            flags |= {a.value for a in node.args if isinstance(a, ast.Constant)}
    assert flags == {"--policy", "--monitor-dir", "--volume", "--protect-only", "--status"}, flags


# ==================================================================================== the launcher: refusal order and promotion
class FakeExecutor:
    def __init__(self):
        self.authority = LiveAuthority.protect_only()
        self.orders_placed = 0

    def summary(self):
        return {"authority": self.authority.mode}


class FakeRuntime:
    def __init__(self):
        self.driver = SimpleNamespace(executor=FakeExecutor())
        self.promotions: list[LiveAuthority] = []
        self.stopped = False

    def promote(self, authority):
        self.promotions.append(authority)
        self.driver.executor.authority = authority


def make_deps(world: World | None, *, typed_digest=None, collector_over=None, terminal=True, calls=None):
    calls = calls if calls is not None else []
    runtime = FakeRuntime()
    stage_holder = {}

    def build_stage(args, policy):
        calls.append(("build_stage", None if policy is None else "policy"))
        stage = ll.Stage(runtime, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(blocked_reason=lambda: None), "/", time.monotonic() - 999,
                         lambda: setattr(runtime, "stopped", True))
        stage_holder["stage"] = stage
        return stage

    def read_digest(what):
        calls.append(("read_digest", what))
        return typed_digest if typed_digest is not None else world.digest

    deps = ll.Deps(read_digest=read_digest, build_stage=build_stage, collectors=lambda stage, policy: collectors(**(collector_over or {})),
                   run_forever=lambda stage: calls.append(("run_forever", stage.runtime.driver.executor.authority.mode)) or 0,
                   sleep=lambda s: None, is_terminal=lambda: terminal)
    return deps, runtime, calls


ARGS = ["--policy", "P", "--monitor-dir", "M"]


def args_for(world):
    return ["--policy", str(world.policy_path), "--monitor-dir", "M"]


def forbid_phrase(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the owner phrase was requested although a gate is not met")
    monkeypatch.setattr(oc, "read_owner_phrase", boom)


def test_with_the_alpha_artifact_absent_nothing_the_owner_types_arms_live(tmp_path, monkeypatch, capsys):
    """THE DEMONSTRATION: every gate but one is real, the owner types the right digest, and the phrase is never even requested."""
    forbid_phrase(monkeypatch)
    w = World(tmp_path, drop=("prospective_green",))
    deps, runtime, calls = make_deps(w)
    assert ll.launch(args_for(w), deps) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["LIVE_DECISION"] == "NO_GO" and out["reason"] == "GATE_NOT_MET:prospective_green" and out["REAL_ORDER_PLACED"] == "NO"
    assert runtime.promotions == [] and runtime.stopped is True and ("run_forever", TRADE) not in calls
    assert runtime.driver.executor.authority.mode == PROTECT_ONLY
    assert "READY" not in capsys.readouterr().out


def test_with_every_gate_but_the_alpha_and_engineering_artifacts_the_phrase_is_still_never_requested(tmp_path, monkeypatch, capsys):
    forbid_phrase(monkeypatch)
    w = World(tmp_path, drop=("prospective_green", "engineering_approval"))
    deps, runtime, _ = make_deps(w)
    assert ll.launch(args_for(w), deps) == 2 and runtime.promotions == []


def test_with_every_gate_present_the_typed_phrase_promotes_the_running_runtime_in_this_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oc, "read_owner_phrase", lambda *a, **k: None)              # a REAL terminal is exercised in the PTY tests below
    w = World(tmp_path, capital=125.0, exposure=500.0)
    deps, runtime, calls = make_deps(w)
    assert ll.launch(args_for(w), deps) == 0
    (auth,) = runtime.promotions
    assert auth.mode == TRADE and auth.capital.capital_ceiling_usdt == 125.0 and auth.capital.exposure_ceiling_usdt == 500.0
    assert oc.is_consumed(auth.identity) and calls[-1] == ("run_forever", TRADE)
    assert "MICRO-LIVE PILOT READY" in capsys.readouterr().out


def test_the_launcher_waits_for_the_proposed_runtime_to_warm_up_before_it_observes_any_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "read_owner_phrase", lambda *a, **k: None)
    w = World(tmp_path)
    deps, runtime, calls = make_deps(w)
    clock = {"t": 1000.0, "slept": 0}
    deps.monotonic = lambda: clock["t"]
    deps.sleep = lambda s: (clock.__setitem__("t", clock["t"] + s), clock.__setitem__("slept", clock["slept"] + s))

    def build_stage(args, policy):
        runtime.stopped = False
        return ll.Stage(runtime, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(blocked_reason=lambda: None), "/", clock["t"],
                        lambda: None)
    deps.build_stage = build_stage
    observed_at = {}
    real = collectors()
    deps.collectors = lambda stage, policy: d.replace(real, feed=lambda: (observed_at.setdefault("t", clock["t"]), Observation(True, {"feed": "OK"}))[1])
    assert ll.launch(args_for(w), deps) == 0
    assert clock["slept"] >= ll.WARMUP_S and observed_at["t"] - 1000.0 >= ll.WARMUP_S          # the feed was observed only after the warm-up


def test_a_wrong_phrase_aborts_and_the_runtime_stays_protect_only(tmp_path, monkeypatch, capsys):
    def wrong(*a, **k):
        raise oc.Refused("PHRASE_MISMATCH")
    monkeypatch.setattr(oc, "read_owner_phrase", wrong)
    w = World(tmp_path)
    deps, runtime, _ = make_deps(w)
    assert ll.launch(args_for(w), deps) == 2
    assert runtime.promotions == [] and runtime.stopped and runtime.driver.executor.authority.mode == PROTECT_ONLY


def test_a_gate_that_turns_bad_after_the_phrase_aborts_before_promotion(tmp_path, monkeypatch):
    seen = {"n": 0}

    def flaky_feed():
        seen["n"] += 1
        return Observation(seen["n"] <= 1, {"feed": "OK" if seen["n"] <= 1 else "UNSAFE"})       # good for the banner, bad afterwards
    monkeypatch.setattr(oc, "read_owner_phrase", lambda *a, **k: None)
    w = World(tmp_path)
    deps, runtime, _ = make_deps(w, collector_over={"feed": flaky_feed})
    assert ll.launch(args_for(w), deps) == 2 and runtime.promotions == []


def test_the_launcher_refuses_without_a_terminal_before_it_even_asks_for_the_digest(tmp_path, capsys):
    w = World(tmp_path)
    deps, runtime, calls = make_deps(w, terminal=False)
    assert ll.launch(args_for(w), deps) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "INTERACTIVE_ONLY"
    assert calls == [] and runtime.promotions == []


@pytest.mark.parametrize("argv,reason", [([], "MONITOR_DIR_REQUIRED"), (["--monitor-dir", "M"], "POLICY_REQUIRED")])
def test_missing_arguments_refuse(argv, reason, capsys):
    deps, runtime, calls = make_deps(None)
    assert ll.launch(argv, deps) == 2 and json.loads(capsys.readouterr().out)["reason"] == reason and calls == []


def test_a_typed_digest_that_does_not_match_the_policy_refuses_before_any_runtime_is_built(tmp_path, capsys):
    w = World(tmp_path)
    deps, runtime, calls = make_deps(w, typed_digest="0" * 64)
    assert ll.launch(args_for(w), deps) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "POLICY_DIGEST_MISMATCH"
    assert not [c for c in calls if c[0] == "build_stage"]


def test_status_evaluates_nothing_and_says_no_go(tmp_path, capsys):
    deps, runtime, calls = make_deps(None)
    assert ll.launch(["--status"], deps) == 2 and calls == []
    out = json.loads(capsys.readouterr().out)
    assert out["LIVE_DECISION"] == "NO_GO" and out["PRODUCTION_ARMED"] == "NO"


def test_protect_only_never_reads_a_digest_never_runs_a_ceremony_and_never_promotes(tmp_path, monkeypatch):
    forbid_phrase(monkeypatch)
    monkeypatch.setattr(oc, "ceremony", lambda p: (_ for _ in ()).throw(AssertionError("no ceremony in PROTECT_ONLY")))
    deps, runtime, calls = make_deps(None)
    deps.ceremony = oc.ceremony
    assert ll.launch(["--protect-only", "--monitor-dir", "M"], deps) == 0
    assert calls == [("build_stage", None), ("run_forever", PROTECT_ONLY)] and runtime.promotions == []


def test_trade_authority_is_built_in_exactly_one_place_and_only_after_the_permit_is_consumed(monkeypatch):
    import ast
    calls = [n for n in ast.walk(_tree(ll)) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "trade"]
    assert len(calls) == 1, "LiveAuthority.trade must have a single call site"
    launch_src = ast.get_source_segment(Path(ll.__file__).read_text(), next(n for n in ast.walk(_tree(ll)) if isinstance(n, ast.FunctionDef) and n.name == "launch"))
    assert launch_src.index("permit.consume(provider)") < launch_src.index("LiveAuthority.trade(")
    for name in ("LSRL_MICROLIVE_TRADE", "LSRL_SKIP_CEREMONY", "OWNER_PHRASE", "START_LIVE_PILOT"):
        monkeypatch.setenv(name, "1")                                                # nothing reads them
    monkeypatch.setattr(oc, "read_owner_phrase", lambda *a, **k: (_ for _ in ()).throw(AssertionError("phrase requested")))
    deps, runtime, calls = make_deps(None, terminal=False)
    assert ll.launch(["--policy", "P", "--monitor-dir", "M"], deps) == 2 and runtime.promotions == []
    with pytest.raises(SystemExit):
        ll.launch(["--policy", "P", "--monitor-dir", "M", "--approve"], deps)         # an unknown flag is an argparse error, not an override


# ==================================================================================== a REAL terminal
CHILD = '''
import os, sys, tempfile, time
from pathlib import Path
from tests.test_lsrl_live_launcher import World, make_deps, args_for
from lsrl.tools import live_launcher as ll
from lsrl.tools import owner_ceremony as oc
orig = oc.read_owner_phrase
oc.read_owner_phrase = lambda: orig(0.6)
orig_digest = oc.read_pinned_digest
tmp = Path(os.environ["WORLD_DIR"])
w = World(tmp, drop=tuple(x for x in os.environ.get("DROP", "").split(",") if x))
deps, runtime, calls = make_deps(w)
deps.read_digest = lambda what: orig_digest(what, 0.6)
rc = ll.launch(args_for(w), deps)
print("RC=%d AUTHORITY=%s PROMOTIONS=%d" % (rc, runtime.driver.executor.authority.mode, len(runtime.promotions)), flush=True)
'''


def run_pty(env_extra, script=CHILD, sends=(), timeout=12):
    """Runs `script` on a genuine controlling terminal; `sends` = [(wait-for-bytes, bytes-to-type)]."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.environ.update(env_extra)
        os.execv(sys.executable, [sys.executable, "-c", script])
    out, todo, deadline = b"", list(sends), time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                while todo and todo[0][0] in out:
                    os.write(fd, todo.pop(0)[1])
            elif b"RC=" in out:
                break
        else:
            raise AssertionError("PTY child hung: " + out.decode(errors="replace")[-400:])
    finally:
        os.close(fd)
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    return out.decode(errors="replace")


DIGEST_TYPING_CHILD = CHILD.replace("w = World(tmp, drop=tuple(x for x in os.environ.get(\"DROP\", \"\").split(\",\") if x))",
                                    "w = World(tmp, drop=tuple(x for x in os.environ.get(\"DROP\", \"\").split(\",\") if x))\nprint('DIGEST_FOR_TEST=' + w.digest, flush=True)")


def drive_owner(tmp_path, *, drop="", phrase=b"START LIVE PILOT\n", timeout=15):
    """A genuine terminal: the 'owner' types the policy digest when asked and the phrase ONLY if the phrase prompt ever appears."""
    (tmp_path / "w").mkdir(exist_ok=True)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.environ["WORLD_DIR"], os.environ["DROP"] = str(tmp_path / "w"), drop
        os.execv(sys.executable, [sys.executable, "-c", DIGEST_TYPING_CHILD])
    out, typed_digest, typed_phrase, deadline = b"", False, False, time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                text = out.decode(errors="replace")
                if not typed_digest and "SHA-256 of the reviewed release policy:" in text and "DIGEST_FOR_TEST=" in text:
                    os.write(fd, (text.split("DIGEST_FOR_TEST=")[1].split()[0] + "\n").encode())
                    typed_digest = True
                if typed_digest and not typed_phrase and "Type exactly START LIVE PILOT:" in text:
                    os.write(fd, phrase)
                    typed_phrase = True
                if "RC=" in text:
                    break
    finally:
        os.close(fd)
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    return out.decode(errors="replace"), typed_digest, typed_phrase


def test_on_a_real_terminal_with_the_alpha_artifact_missing_the_phrase_is_never_asked_for_and_nothing_arms(tmp_path):
    text, typed_digest, typed_phrase = drive_owner(tmp_path, drop="prospective_green")
    assert typed_digest and not typed_phrase                                         # the prompt for the phrase never appeared
    assert "GATE_NOT_MET:prospective_green" in text and "Type exactly START LIVE PILOT" not in text and "MICRO-LIVE PILOT READY" not in text
    assert "RC=2 AUTHORITY=PROTECT_ONLY PROMOTIONS=0" in text and '"LIVE_DECISION": "NO_GO"' in text


def test_on_a_real_terminal_a_wrong_phrase_after_every_gate_passed_still_arms_nothing(tmp_path):
    text, typed_digest, typed_phrase = drive_owner(tmp_path, phrase=b"start live pilot\n")
    assert typed_digest and typed_phrase and "PHRASE_MISMATCH" in text
    assert "RC=2 AUTHORITY=PROTECT_ONLY PROMOTIONS=0" in text


def test_on_a_real_terminal_a_typed_phrase_where_the_digest_is_asked_is_refused(tmp_path):
    (tmp_path / "w").mkdir()
    out = run_pty({"WORLD_DIR": str(tmp_path / "w")}, sends=[(b"SHA-256 of the reviewed release policy:", b"START LIVE PILOT\n")])
    assert "DIGEST_MALFORMED" in out and "AUTHORITY=PROTECT_ONLY PROMOTIONS=0" in out and "Type exactly START LIVE PILOT" not in out


def test_on_a_real_terminal_the_full_ceremony_promotes_only_when_the_owner_types_the_digest_then_the_exact_phrase(tmp_path):
    text, typed_digest, typed_phrase = drive_owner(tmp_path)
    assert typed_digest and typed_phrase and "MICRO-LIVE PILOT READY" in text and "RC=0 AUTHORITY=TRADE PROMOTIONS=1" in text, text[-600:]


def test_the_real_launcher_today_is_no_go_on_a_pipe_and_on_a_terminal(tmp_path):
    """No fakes at all: the shipped module, no policy on disk. A piped phrase and a typed one both end NO_GO; no runtime is built, no network is touched."""
    piped = subprocess.run([sys.executable, "-m", "lsrl.tools.live_launcher", "--policy", str(tmp_path / "policy.json"), "--monitor-dir", str(tmp_path)],
                           input=oc.PHRASE + "\n" + "a" * 64 + "\n", text=True, capture_output=True, timeout=30, cwd=ROOT)
    out = json.loads(piped.stdout.strip().splitlines()[-1])
    assert piped.returncode == 2 and out["LIVE_DECISION"] == "NO_GO" and out["reason"] == "INTERACTIVE_ONLY" and out["REAL_ORDER_PLACED"] == "NO"
    tty = run_pty({}, script="import sys; from lsrl.tools import live_launcher as ll; rc = ll.launch(['--policy', %r, '--monitor-dir', %r]); print('RC=%%d' %% rc, flush=True)"
                   % (str(tmp_path / "policy.json"), str(tmp_path)),
                   sends=[(b"SHA-256 of the reviewed release policy:", ("a" * 64 + "\n").encode())])
    assert "POLICY_NOT_PRESENT" in tty and "RC=2" in tty and "Type exactly START LIVE PILOT" not in tty


# ==================================================================================== the collectors
class St:
    def __init__(self, **over):
        self.doc = {"mode": "MICRO_LIVE", "engine_halted": None, "recorder_error": None, "feed": {"state": "OK"},
                    "ws": {"connected": True, "last_frame_age_s": 0.4}}
        self.doc.update(over)
        self.driver = SimpleNamespace(recorder=object(), executor=SimpleNamespace(summary=lambda: dict(SUMMARY), gate=None))

    def status(self):
        return self.doc


SUMMARY = {"authority": "PROTECT_ONLY", "halt_reason": None, "emergency": False, "recovered": True, "exposure_state": "NONE", "startup_unreadable": False,
           "unclassified_orders": 0, "foreign_plans": 0, "cancel_failures": 0, "open_setups": 0}


def test_observe_turns_any_exception_or_non_observation_into_a_failed_gate():
    assert gc.observe(lambda: (_ for _ in ()).throw(RuntimeError("x" * 40))).passed is False
    assert gc.observe(lambda: True).passed is False
    assert gc.observe(lambda: Observation(True, {})).passed is True


def test_feed_gate_needs_an_ok_connected_fresh_warm_runtime():
    assert gc.feed_gate(St(), uptime_s=120).passed
    for over, uptime in (({"feed": {"state": "UNSAFE"}}, 120), ({"ws": {"connected": False, "last_frame_age_s": 0.1}}, 120),
                         ({"ws": {"connected": True, "last_frame_age_s": 30}}, 120), ({"engine_halted": "X"}, 120), ({}, 10)):
        assert not gc.feed_gate(St(**over), uptime_s=uptime).passed


def test_recorder_gate():
    assert gc.recorder_gate(St()).passed and not gc.recorder_gate(St(recorder_error="disk floor")).passed
    s = St()
    s.driver.recorder = None
    assert not gc.recorder_gate(s).passed


def test_disk_gate_needs_space_and_a_quiet_monitor():
    usage = lambda v: SimpleNamespace(free=20e9)          # noqa: E731
    assert gc.disk_gate(SimpleNamespace(blocked_reason=lambda: None), "/", disk_usage=usage).passed
    assert not gc.disk_gate(SimpleNamespace(blocked_reason=lambda: "ACTIVE_ALARMS:DRAIN"), "/", disk_usage=usage).passed
    assert not gc.disk_gate(SimpleNamespace(blocked_reason=lambda: None), "/", disk_usage=lambda v: SimpleNamespace(free=2e9)).passed


def test_clock_gate_is_the_same_gate_every_order_passes():
    good = SimpleNamespace(_require_safe_clock_for_order=lambda: None)
    assert gc.observe(lambda: gc.clock_gate(good)).passed
    from clients.clock_safety import ClockUnsafe
    bad = SimpleNamespace(_require_safe_clock_for_order=lambda: (_ for _ in ()).throw(ClockUnsafe("stale")))
    assert not gc.observe(lambda: gc.clock_gate(bad)).passed


def test_risk_manager_gate_wants_the_central_gate_of_that_very_risk_manager():
    from lsrl.adapters.riskgate import CentralRiskGate
    from lsrl.core.gates import NullRiskGate
    rm = SimpleNamespace(evaluate=lambda *a, **k: None)
    s = St()
    s.driver.executor.gate = CentralRiskGate(rm, SimpleNamespace())
    assert gc.risk_manager_gate(s, rm).passed
    assert not gc.risk_manager_gate(s, SimpleNamespace(evaluate=lambda: None)).passed         # a different risk manager
    s.driver.executor.gate = NullRiskGate()
    assert not gc.risk_manager_gate(s, rm).passed


def test_kill_switch_gate_needs_configured_readable_limits():
    ok = SimpleNamespace(_daily_defensive_status=lambda: {"daily_total_net_pnl": 0.0}, _weekly_realized_pnl=lambda: 0.0)
    cfg = SimpleNamespace(hard_daily_stop_pct=5.0, weekly_freeze_loss_pct=10.0)
    assert gc.kill_switch_gate(ok, cfg).passed
    assert not gc.kill_switch_gate(ok, SimpleNamespace(hard_daily_stop_pct=0.0, weekly_freeze_loss_pct=10.0)).passed
    assert not gc.kill_switch_gate(ok, SimpleNamespace(hard_daily_stop_pct=5.0, weekly_freeze_loss_pct=0.0)).passed
    unreadable = SimpleNamespace(_daily_defensive_status=lambda: {"daily_status_unreadable": True}, _weekly_realized_pnl=lambda: 0.0)
    assert not gc.kill_switch_gate(unreadable, cfg).passed
    broken = SimpleNamespace(_daily_defensive_status=lambda: {}, _weekly_realized_pnl=lambda: (_ for _ in ()).throw(OSError("no dataset")))
    assert not gc.kill_switch_gate(broken, cfg).passed


@pytest.mark.parametrize("over", [{"recovered": False}, {"halt_reason": "X"}, {"emergency": True}, {"exposure_state": "UNPROTECTED"}, {"startup_unreadable": True},
                                  {"unclassified_orders": 1}, {"foreign_plans": 1}, {"cancel_failures": 1}, {"open_setups": 1}])
def test_reconciliation_gate_requires_a_clean_recovered_executor(over):
    assert gc.reconciliation_gate(St()).passed
    s = St()
    s.driver.executor.summary = lambda: {**SUMMARY, **over}
    assert not gc.reconciliation_gate(s).passed


def test_exchange_protection_gate_is_the_audit_saying_nothing_is_exposed_or_orphaned():
    assert gc.exchange_protection_gate(CLEAN_AUDIT).passed
    for over in ({"complete": False}, {"REAL_POSITIONS": 1}, {"REAL_ORDERS": 1}, {"REAL_PLAN_ORDERS": 1}):
        assert not gc.exchange_protection_gate({**CLEAN_AUDIT, **over}).passed


def test_the_real_collectors_are_all_wired_and_read_only(monkeypatch):
    stage = ll.Stage(St(), SimpleNamespace(lsrl_accept_demo_process_down_evidence=True), SimpleNamespace(_require_safe_clock_for_order=lambda: None),
                     SimpleNamespace(), SimpleNamespace(blocked_reason=lambda: None), "/", time.monotonic() - 1000, lambda: None)
    stage.runtime.equity_fn = lambda: 500.0
    col = ll.real_collectors(stage, SimpleNamespace(capital=CapitalPolicy(125, 500)))
    assert set(d.fields(Collectors) and [f.name for f in d.fields(Collectors)]) == set(vars(col))
    assert col.equity() == 500.0 and col.accept_demo() is True and col.clock().passed and col.feed().passed
    assert canonical_digest({"a": 1, "b": [2]}) == canonical_digest({"b": [2], "a": 1})            # order-independent evidence digests
