"""The launch checklist as a program: what exists on disk right now, and therefore what is still missing. It changes nothing and can arm nothing.

    .venv/bin/python -m lsrl.tools.readiness_report [--policy P --policy-sha256 D] [--monitor-dir M] [--paper-dir DIR]

Every line is COMPUTED from a file or a self-check; nothing is hard-coded to a colour. `ALPHA_EVIDENCE_GATE` is PASS only if the release policy pins a
`prospective_green` artifact that verifies (decision PASS, unexpired, bound to this release SHA and the frozen config, reproduced by Agent C). Engineering cannot
override missing alpha evidence: with no such artifact the last two lines are `ALPHA_EVIDENCE_GATE=FAIL` and `LIVE_DECISION=NO_GO`, whatever else is green. Even with
every line green the decision is only `OWNER_CEREMONY_REQUIRED`: the owner still has to type the policy digest and the phrase, on a terminal, to `lsrl.tools.live_launcher`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from lsrl.tools import owner_ceremony as oc
from lsrl.tools.release_policy import ARTIFACT_GATES, PolicyRefused, load_policy

BINDINGS = {
    "prospective_green": lambda sha, policy: {"kind": "LSRL_PROSPECTIVE_GREEN", "protocol_id": "LSRL_PROSPECTIVE_PROTOCOL_V1", "git_sha": sha,
                                              "config_digest": oc.CONFIG_HASH, "reproduced_by": "AGENT_C"},
    "engineering_approval": lambda sha, policy: {"kind": "LSRL_ENGINEERING_APPROVAL", "git_sha": sha, "reviewer": "AGENT_C"},
    "exchange_semantics": lambda sha, policy: {"kind": "LSRL_EXCHANGE_SEMANTICS_REPRODUCTION", "git_sha": sha, "reproduced_by": "AGENT_C", "read_only": True,
                                               "attestation_sha256": policy.artifacts["process_down_protection"].sha256},
}


def _age(path: Path, now: float) -> float | None:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def self_checks() -> dict[str, str]:
    """Cheap behavioural checks that the safety structure is in this tree. They construct nothing that could trade and touch no exchange."""
    out: dict[str, str] = {}

    def refuses(label: str, fn: Any, exc: type[BaseException]) -> None:
        try:
            fn()
            out[label] = "FAIL"
        except exc:
            out[label] = "PASS"
        except Exception as other:                               # noqa: BLE001
            out[label] = f"ERROR:{type(other).__name__}"

    from lsrl.adapters.authority import TRADE, AuthorityRefused, LiveAuthority
    from lsrl.adapters.process_down import AttestationRefused, verify_attestation
    from lsrl.adapters.strategy_port import IntentRefused, check_intents
    from lsrl.capital import CapitalPolicy, CapitalRefused
    refuses("attestation_refuses_missing_file", lambda: verify_attestation("/nonexistent", "0" * 64, code_sha="0" * 40), AttestationRefused)
    refuses("ceremony_refuses_empty_snapshot", lambda: oc.validate(oc.Snapshot(None, None, {}, (), (), (), True)), oc.Refused)     # type: ignore[arg-type]
    refuses("trade_authority_needs_a_consumed_permit", lambda: LiveAuthority(TRADE, object(), None, None), AuthorityRefused)
    refuses("capital_below_the_band_refused", lambda: CapitalPolicy(99.0, 100.0), CapitalRefused)
    refuses("capital_above_the_band_refused", lambda: CapitalPolicy(151.0, 100.0), CapitalRefused)
    refuses("unknown_intent_refused", lambda: check_intents([object()]), IntentRefused)
    out["a_copied_identity_is_not_consumed"] = "PASS" if not oc.is_consumed(object()) else "FAIL"
    return out


def checklist(root: Path, *, policy_path: Path | None, policy_digest: str | None, monitor_dir: Path | None, paper_dir: Path | None,
              now: float | None = None) -> list[tuple[str, str, str]]:
    now = time.time() if now is None else now
    rows: list[tuple[str, str, str]] = []
    try:
        facts = oc.git_facts(root)
    except Exception:                                            # noqa: BLE001
        facts = {"sha": "UNKNOWN", "clean": False, "strategy_freeze": False}
    rows.append(("RELEASE_SHA", "OK" if len(facts["sha"]) == 40 else "UNKNOWN", facts["sha"]))
    rows.append(("TREE_CLEAN", "OK" if facts["clean"] else "NOT_OK", ""))
    rows.append(("STRATEGY_FROZEN_AT_BASE", "OK" if facts["strategy_freeze"] else "NOT_OK", oc.BASE_SHA))
    policy = None
    if policy_path is None or policy_digest is None:
        rows.append(("RELEASE_POLICY", "ABSENT", "no policy file / typed digest supplied; the owner types the digest only at the launcher's terminal"))
    else:
        try:
            policy = load_policy(policy_path, policy_digest)
            rows.append(("RELEASE_POLICY", "VERIFIED", f"capital_ceiling={policy.capital.capital_ceiling_usdt} (the owner's choice)"))
        except PolicyRefused as exc:
            rows.append(("RELEASE_POLICY", "REFUSED", str(exc)))
    alpha = "FAIL"
    for name in ("prospective_green", "engineering_approval", "exchange_semantics"):
        if policy is None:
            status = "ABSENT"
        else:
            ok = oc.pinned_artifact(policy.artifacts[name].path, policy.artifacts[name].sha256, BINDINGS[name](policy.approved_sha, policy))
            status = "VERIFIED" if ok else "MISSING_OR_INVALID"
        rows.append((f"{name.upper()}_ARTIFACT", status, ""))
        if name == "prospective_green" and status == "VERIFIED":
            alpha = "PASS"
    if policy is None:
        rows.append(("PROCESS_DOWN_ATTESTATION", "ABSENT", "produced by the owner-run demo probe; see the owner instructions"))
    else:
        from lsrl.adapters.process_down import AttestationRefused, verify_attestation
        try:
            got = verify_attestation(policy.artifacts["process_down_protection"].path, policy.artifacts["process_down_protection"].sha256,
                                     code_sha=policy.approved_sha, now=now, accept_demo=True)
            rows.append(("PROCESS_DOWN_ATTESTATION", "VERIFIED", f"model={got.observed_model} environment={got.environment}"))
        except AttestationRefused as exc:
            rows.append(("PROCESS_DOWN_ATTESTATION", "REFUSED", str(exc)[:100]))
    rows.append(("CAPITAL_CEILING", "OWNER_APPROVAL_REQUIRED" if policy is None else "OWNER_SET", "100-150 USDT band; nothing chooses it for the owner"))
    if monitor_dir is not None:
        from lsrl.adapters.capacity_gate import MonitorCapacityGate
        reason = MonitorCapacityGate(monitor_dir, now=lambda: now).blocked_reason()
        rows.append(("CAPACITY_MONITOR", "OK" if reason is None else "NOT_OK", reason or ""))
    else:
        rows.append(("CAPACITY_MONITOR", "NOT_EVALUATED", "no --monitor-dir"))
    if paper_dir is not None:
        age = _age(paper_dir / "status.json", now)
        rows.append(("PAPER_COLLECTION", "RUNNING" if age is not None and age < 120 else "NOT_RUNNING", "" if age is None else f"status.json age {age:.0f}s"))
    else:
        rows.append(("PAPER_COLLECTION", "NOT_EVALUATED", "no --paper-dir"))
    rows.append(("EXCHANGE_EXPOSURE", "NOT_EVALUATED", "the owner runs lsrl.tools.exposure_audit with authenticated read-only access"))
    for name, status in self_checks().items():
        rows.append((name.upper(), status, "self-check"))
    # evaluated by the launcher on the live runtime, not here; the alpha artifact is judged on its own line so that "everything else" is INDEPENDENT of it
    owner_or_live = ("CAPACITY_MONITOR", "PAPER_COLLECTION", "EXCHANGE_EXPOSURE", "PROSPECTIVE_GREEN_ARTIFACT")
    everything = all(s in ("OK", "VERIFIED", "OWNER_SET", "PASS") for _n, s, _d in rows if _n not in owner_or_live)
    rows.append(("ALPHA_EVIDENCE_GATE", alpha, "" if alpha == "PASS" else "no verified prospective_green artifact: engineering cannot override missing alpha evidence"))
    rows.append(("LIVE_DECISION", "OWNER_CEREMONY_REQUIRED" if (alpha == "PASS" and everything) else "NO_GO", ""))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy", type=Path)
    ap.add_argument("--policy-sha256")
    ap.add_argument("--monitor-dir", type=Path)
    ap.add_argument("--paper-dir", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = checklist(Path(__file__).resolve().parents[2], policy_path=args.policy, policy_digest=args.policy_sha256,
                     monitor_dir=args.monitor_dir, paper_dir=args.paper_dir)
    if args.json:
        print(json.dumps([{"item": n, "status": s, "detail": d} for n, s, d in rows], indent=1))
    else:
        for n, s, d in rows[:-2]:
            print(f"{n}={s}" + (f"   # {d}" if d else ""))
        for n, s, d in rows[-2:]:
            print(f"{n}={s}" + (f"   # {d}" if d else ""))
    return 0 if rows[-1][1] == "OWNER_CEREMONY_REQUIRED" else 2


if __name__ == "__main__":
    sys.exit(main())
