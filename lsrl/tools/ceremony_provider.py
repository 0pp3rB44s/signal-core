"""The PRODUCTION ceremony provider: turns a typed-digest release policy, independently issued artifacts and live observations of the PROPOSED runtime into
the `Snapshot` the ceremony validates. It computes every one of the 19 gates; it never grants one.

  * the ALPHA gate (`prospective_green`) and the ENGINEERING gate (`engineering_approval`) are artifacts that must exist, be pinned by the release policy, carry
    `decision: PASS`, be unexpired and be bound to THIS release SHA and frozen config. No such artifact exists today, so both are False and the ceremony
    refuses BEFORE it asks the owner for anything. There is no flag, file, variable or phrase that substitutes for them;
  * process-down protection is the attestation, verified exactly as MICRO_LIVE itself verifies it;
  * exposure gates come from the account-wide read-only audit (`lsrl.tools.exposure_audit`);
  * clock, feed, recorder, disk, risk manager, kill switches and reconciliation are observed on the proposed runtime by the collectors.

A collector that raises is a failed gate. Collectors run in parallel and each is stamped when IT finishes, so the whole snapshot fits the ceremony's freshness window.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lsrl.adapters.process_down import AttestationRefused, verify_attestation
from lsrl.tools import owner_ceremony as oc
from lsrl.tools.gate_collectors import Observation, observe
from lsrl.tools.release_policy import ReleasePolicy

PROTOCOL_ID = "LSRL_PROSPECTIVE_PROTOCOL_V1"
REVIEWER = "AGENT_C"


def canonical_digest(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Collectors:
    """Everything the provider observes, injected so the wiring is testable without an exchange. Real ones: `lsrl.tools.live_launcher.real_collectors`."""
    git: Callable[[], dict]
    equity: Callable[[], float]
    runtime_config_hash: Callable[[ReleasePolicy], str]
    accept_demo: Callable[[], bool]
    audit: Callable[[], dict]
    clock: Callable[[], Observation]
    feed: Callable[[], Observation]
    recorder: Callable[[], Observation]
    disk: Callable[[], Observation]
    risk_manager: Callable[[], Observation]
    kill_switches: Callable[[], Observation]
    reconciliation: Callable[[], Observation]


class ProductionProvider:
    def __init__(self, policy: ReleasePolicy, collectors: Collectors, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.policy, self.c, self._mono, self._wall = policy, collectors, clock, wall

    # ---------------------------------------------------------------- artifacts (bound gates)
    def _artifact(self, name: str, binding: dict[str, Any]) -> Observation:
        ref = self.policy.artifacts[name]
        ok = oc.pinned_artifact(ref.path, ref.sha256, binding)
        return Observation(ok, {"artifact": name, "sha256": ref.sha256, "verified_content": ok})

    def _attestation(self, sha: str) -> Observation:
        ref = self.policy.artifacts["process_down_protection"]
        try:
            granted = verify_attestation(ref.path, ref.sha256, code_sha=sha, now=self._wall(), accept_demo=bool(self.c.accept_demo()))
        except AttestationRefused as exc:
            return Observation(False, {"artifact": "process_down_protection", "sha256": ref.sha256, "refused": str(exc)[:120]})
        return Observation(True, {"artifact": "process_down_protection", "sha256": ref.sha256, "model": granted.observed_model,
                                  "environment": granted.environment, "place_first": granted.plan_place_first_ok})

    def snapshot(self) -> oc.Snapshot:
        p, c = self.policy, self.c
        stamped: dict[str, oc.Evidence] = {}

        git = observe(lambda: Observation(True, dict(c.git()))).doc
        git_at = self._mono()
        sha = str(git.get("sha") or "UNKNOWN")
        nav_obs: dict[str, Any] = {}

        def nav() -> None:
            nav_obs["equity"] = float(c.equity())
        try:
            nav()
        except Exception:                                      # noqa: BLE001 - an unreadable equity leaves NAV at 0 -> CAPITAL_UNKNOWN
            nav_obs["equity"] = 0.0
        nav_usdt = min(nav_obs["equity"], p.capital.capital_ceiling_usdt)
        try:
            runtime_hash = str(c.runtime_config_hash(p))
        except Exception:                                      # noqa: BLE001
            runtime_hash = "0" * 64
        runtime_at = self._mono()
        from lsrl.core.config import DEFAULT_CONFIG
        identity = oc.Identity(sha if len(sha) == 40 else "0" * 40, DEFAULT_CONFIG.digest(), runtime_hash, p.account, nav_usdt,
                               p.capital.capital_ceiling_usdt, p.capital.exposure_ceiling_usdt)
        approved = oc.Identity(p.approved_sha, p.config_hash, p.runtime_config_hash, p.account, nav_usdt,
                               p.capital.capital_ceiling_usdt, p.capital.exposure_ceiling_usdt)

        jobs: dict[str, Callable[[], Observation]] = {
            "clock": c.clock, "feed": c.feed, "recorder": c.recorder, "disk": c.disk, "risk_manager": c.risk_manager,
            "kill_switches": c.kill_switches, "reconciliation": c.reconciliation,
            "audit": lambda: Observation(True, dict(c.audit())),
            "prospective_green": lambda: self._artifact("prospective_green", {
                "kind": "LSRL_PROSPECTIVE_GREEN", "protocol_id": PROTOCOL_ID, "git_sha": identity.sha, "config_digest": oc.CONFIG_HASH,
                "reproduced_by": REVIEWER}),
            "engineering_approval": lambda: self._artifact("engineering_approval", {
                "kind": "LSRL_ENGINEERING_APPROVAL", "git_sha": identity.sha, "reviewer": REVIEWER}),
            "exchange_semantics": lambda: self._artifact("exchange_semantics", {
                "kind": "LSRL_EXCHANGE_SEMANTICS_REPRODUCTION", "git_sha": identity.sha, "reproduced_by": REVIEWER, "read_only": True,
                "attestation_sha256": p.artifacts["process_down_protection"].sha256}),
            "process_down_protection": lambda: self._attestation(identity.sha),
        }
        results: dict[str, Observation] = {}
        stamps: dict[str, float] = {}

        def run(name: str, fn: Callable[[], Observation]) -> None:
            results[name] = observe(fn)
            stamps[name] = self._mono()

        # Two phases so the ceremony's 5 s freshness window is never the account-wide audit's problem: the slow reads (audit, artifacts) run first, in
        # parallel; the LIVE health observations run after them, so the evidence that changes fastest is also the freshest at validation time.
        slow = ("audit", "prospective_green", "engineering_approval", "exchange_semantics", "process_down_protection")
        for group in (slow, tuple(n for n in jobs if n not in slow)):
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                for future in [pool.submit(run, n, jobs[n]) for n in group]:
                    future.result()

        def put(name: str, obs: Observation, *, artifact: str | None = None, at: float | None = None) -> None:
            """An observation is stamped when IT was made: derived gates carry the time of what they were derived from, never 'now'."""
            source = f"artifact:{artifact}" if artifact else f"observation:{canonical_digest({'gate': name, **obs.doc})}"
            stamped[name] = oc.Evidence(bool(obs.passed), at if at is not None else stamps.get(name, self._mono()), source)

        for name in ("clock", "feed", "recorder", "disk", "risk_manager", "kill_switches", "reconciliation"):
            put(name, results[name])
        for name in ("prospective_green", "engineering_approval", "exchange_semantics", "process_down_protection"):
            put(name, results[name], artifact=p.artifacts[name].sha256)
        audit = results["audit"].doc if results["audit"].passed else {"complete": False, "errors": ["audit collector failed"]}
        complete = bool(audit.get("complete"))
        exposure_free = complete and audit.get("REAL_POSITIONS") == 0 and audit.get("REAL_ORDERS") == 0 and audit.get("REAL_PLAN_ORDERS") == 0
        digest_view = {k: audit.get(k) for k in ("complete", "REAL_POSITIONS", "REAL_ORDERS", "REAL_PLAN_ORDERS", "EXCHANGE_EXPOSURE_GATE", "scope")}
        audit_at = stamps["audit"]
        put("authenticated_exposure", Observation(complete, digest_view), at=audit_at)
        put("no_unexpected_exposure", Observation(bool(exposure_free), digest_view), at=audit_at)
        put("exchange_protection", Observation(bool(exposure_free), digest_view), at=audit_at)
        put("approved_sha", Observation(sha == p.approved_sha and len(sha) == 40, {"head": sha, "approved": p.approved_sha}), at=git_at)
        put("clean_tree", Observation(bool(git.get("clean")), {"clean": bool(git.get("clean"))}), at=git_at)
        put("strategy_freeze", Observation(bool(git.get("strategy_freeze")), {"strategy_freeze": bool(git.get("strategy_freeze"))}), at=git_at)
        put("approved_config", Observation(DEFAULT_CONFIG.digest() == p.config_hash == oc.CONFIG_HASH and runtime_hash == p.runtime_config_hash,
                                           {"config_hash": DEFAULT_CONFIG.digest(), "runtime_config_hash": runtime_hash}), at=runtime_at)
        put("owner_capital_approval", Observation(True, {"policy": p.sha256}), artifact=p.sha256)
        rows = lambda key: tuple((r.get("product_type"), r.get("symbol"), r.get("side") or r.get("order_id")) for r in (audit.get(key) or []))   # noqa: E731
        return oc.Snapshot(identity, approved, {n: stamped[n] for n in oc.GATES if n in stamped}, rows("positions"), rows("orders"),
                           rows("plan_orders"), complete,
                           {**{n: p.artifacts[n].sha256 for n in p.artifacts}, "owner_capital_approval": p.sha256})
