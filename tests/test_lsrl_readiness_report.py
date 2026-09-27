"""The launch checklist as a program: alpha evidence is computed from a verified artifact, engineering cannot override its absence."""
from __future__ import annotations

import json
import time

import pytest

from lsrl.tools import owner_ceremony as oc
from lsrl.tools import readiness_report as rr
from tests.test_lsrl_live_launcher import World, SHA


def status(rows):
    return {n: s for n, s, _d in rows}


@pytest.fixture(autouse=True)
def good_git(monkeypatch):
    monkeypatch.setattr(oc, "git_facts", lambda root: {"sha": SHA, "clean": True, "strategy_freeze": True})


def test_today_with_nothing_on_disk_it_ends_alpha_fail_and_no_go_and_exits_nonzero(tmp_path, capsys):
    assert rr.main([]) == 2
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-2].startswith("ALPHA_EVIDENCE_GATE=FAIL") and lines[-1] == "LIVE_DECISION=NO_GO"
    assert "RELEASE_POLICY=ABSENT" in "\n".join(lines) and "PROCESS_DOWN_ATTESTATION=ABSENT" in "\n".join(lines)
    assert "CAPITAL_CEILING=OWNER_APPROVAL_REQUIRED" in "\n".join(lines)


def test_the_structural_self_checks_all_pass_in_this_tree():
    checks = rr.self_checks()
    assert set(checks) == {"attestation_refuses_missing_file", "ceremony_refuses_empty_snapshot", "trade_authority_needs_a_consumed_permit",
                           "capital_below_the_band_refused", "capital_above_the_band_refused", "unknown_intent_refused", "a_copied_identity_is_not_consumed"}
    assert set(checks.values()) == {"PASS"}, checks


def test_engineering_cannot_override_missing_alpha_evidence(tmp_path):
    """Everything else is genuinely present and verified: the alpha artifact alone is missing, and the decision is still NO_GO."""
    w = World(tmp_path, drop=("prospective_green",))
    rows = rr.checklist(SHA and __import__("pathlib").Path("."), policy_path=w.policy_path, policy_digest=w.digest, monitor_dir=None, paper_dir=None)
    s = status(rows)
    assert s["ENGINEERING_APPROVAL_ARTIFACT"] == "VERIFIED" and s["EXCHANGE_SEMANTICS_ARTIFACT"] == "VERIFIED" and s["PROCESS_DOWN_ATTESTATION"] == "VERIFIED"
    assert s["PROSPECTIVE_GREEN_ARTIFACT"] == "MISSING_OR_INVALID" and s["ALPHA_EVIDENCE_GATE"] == "FAIL" and s["LIVE_DECISION"] == "NO_GO"


def test_even_with_every_artifact_verified_the_decision_is_only_that_the_owner_ceremony_is_required(tmp_path):
    w = World(tmp_path)
    rows = rr.checklist(__import__("pathlib").Path("."), policy_path=w.policy_path, policy_digest=w.digest, monitor_dir=None, paper_dir=None)
    s = status(rows)
    assert s["ALPHA_EVIDENCE_GATE"] == "PASS" and s["LIVE_DECISION"] == "OWNER_CEREMONY_REQUIRED" and s["CAPITAL_CEILING"] == "OWNER_SET"
    assert s["EXCHANGE_EXPOSURE"] == "NOT_EVALUATED"                                       # only the owner, with credentials, can evaluate it


def test_a_wrong_typed_digest_or_a_tampered_artifact_does_not_verify(tmp_path):
    w = World(tmp_path)
    s = status(rr.checklist(__import__("pathlib").Path("."), policy_path=w.policy_path, policy_digest="0" * 64, monitor_dir=None, paper_dir=None))
    assert s["RELEASE_POLICY"] == "REFUSED" and s["ALPHA_EVIDENCE_GATE"] == "FAIL" and s["LIVE_DECISION"] == "NO_GO"
    doc = json.loads(w.paths["prospective_green"].read_text())
    doc["decision"] = "PASS "
    w.paths["prospective_green"].write_text(json.dumps(doc))
    s = status(rr.checklist(__import__("pathlib").Path("."), policy_path=w.policy_path, policy_digest=w.digest, monitor_dir=None, paper_dir=None))
    assert s["PROSPECTIVE_GREEN_ARTIFACT"] == "MISSING_OR_INVALID" and s["ALPHA_EVIDENCE_GATE"] == "FAIL"


def test_the_paper_and_monitor_lines_are_read_from_files(tmp_path):
    now = time.time()
    (tmp_path / "status.json").write_text("{}")
    (tmp_path / "monitor_status.json").write_text(json.dumps({"t": now, "alive": True, "interval_s": 60}))
    (tmp_path / "ALARM.json").write_text(json.dumps({"t": now, "active": {}}))
    s = status(rr.checklist(__import__("pathlib").Path("."), policy_path=None, policy_digest=None, monitor_dir=tmp_path, paper_dir=tmp_path, now=now))
    assert s["PAPER_COLLECTION"] == "RUNNING" and s["CAPACITY_MONITOR"] == "OK"
    s = status(rr.checklist(__import__("pathlib").Path("."), policy_path=None, policy_digest=None, monitor_dir=tmp_path, paper_dir=tmp_path, now=now + 3600))
    assert s["PAPER_COLLECTION"] == "NOT_RUNNING" and s["CAPACITY_MONITOR"] == "NOT_OK"


def test_alpha_present_but_a_refused_attestation_is_still_no_go(tmp_path):
    """Alpha PASS alone is not enough either: every other artifact must verify before the decision is even 'owner ceremony required'."""
    w = World(tmp_path)
    doc = json.loads(w.paths["process_down_protection"].read_text())
    doc["decision"] = "FAIL"
    w.paths["process_down_protection"].write_text(json.dumps(doc))
    s = status(rr.checklist(__import__("pathlib").Path("."), policy_path=w.policy_path, policy_digest=w.digest, monitor_dir=None, paper_dir=None))
    assert s["ALPHA_EVIDENCE_GATE"] == "PASS" and s["PROCESS_DOWN_ATTESTATION"] == "REFUSED" and s["LIVE_DECISION"] == "NO_GO"
