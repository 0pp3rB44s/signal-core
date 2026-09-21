"""Agent C's 36-case start_bot.sh matrix (2026-09-20), ported to the hermetic harness and re-run against the launcher that
now asks the APP's own settings resolver, plus the three constructed bypass cases (G: duplicate-case variables, E16:
${VAR} interpolation, E15: the boolean token `t`).

For EVERY case two things are checked:
  * the launcher's decision (refused / started) equals what the APP resolves for that exact environment
    (`Settings()` resolved through bash, like the real launch) -- the guard can no longer disagree with the app;
  * where the case has an intended answer, that answer holds; and no secret value ever appears in any output.
"""
from __future__ import annotations

import os

import pytest

from tests.start_bot_harness import live_capable, refused, run_start_bot, spawn_decoy

CANARY = "CANARYSECRET_9f3a"
PAPER = ("EXECUTION_MODE=DRY_RUN\nEXECUTION_ENABLED=false\nBITGET_API_KEY=\nBITGET_API_SECRET=\n"
         "BITGET_API_PASSPHRASE=\nAPP_MODE=paper\n")

# (id, env_text, os_env, expect_refuse)      expect_refuse=None -> only 'agrees with the app' is asserted
CASES = [
    ("A benign paper .env, clean shell", PAPER, [], False),
    ("B .env benign + OS EXECUTION_MODE=LIVE", PAPER, [("EXECUTION_MODE", "LIVE")], True),
    ("C NO .env, live variables exported (the original bypass)", None,
     [("EXECUTION_MODE", "LIVE"), ("EXECUTION_ENABLED", "true"), ("BITGET_API_KEY", CANARY),
      ("BITGET_API_SECRET", CANARY), ("BITGET_API_PASSPHRASE", CANARY)], True),
    ("C2 NO .env, only credentials exported", None, [("BITGET_API_KEY", CANARY)], True),
    ("C3 NO .env, only EXECUTION_ENABLED=true", None, [("EXECUTION_ENABLED", "true")], True),
    ("D1 OS execution_mode=LIVE", PAPER, [("execution_mode", "LIVE")], True),
    ("D2 OS Execution_Mode=live", PAPER, [("Execution_Mode", "live")], True),
    ("D3 OS execution_enabled=TRUE", PAPER, [("execution_enabled", "TRUE")], True),
    ("D4 OS bitget_api_key=CANARY", PAPER, [("bitget_api_key", CANARY)], True),
    ("D5 OS Bitget_Api_Secret=CANARY", PAPER, [("Bitget_Api_Secret", CANARY)], True),
    ("E1 export EXECUTION_MODE=LIVE", "export EXECUTION_MODE=LIVE\n", [], True),
    ('E2 EXECUTION_MODE="LIVE"', 'EXECUTION_MODE="LIVE"\n', [], True),
    ("E3 EXECUTION_MODE='live'", "EXECUTION_MODE='live'\n", [], True),
    ("E4 EXECUTION_MODE = LIVE (spaces)", "EXECUTION_MODE = LIVE\n", [], True),
    ("E5 EXECUTION_MODE=LIVE # trailing comment", "EXECUTION_MODE=LIVE # x\n", [], True),
    ("E6 lowercase key in file", "execution_mode=LIVE\n", [], True),
    ("E7 CRLF line endings", b"EXECUTION_MODE=LIVE\r\nEXECUTION_ENABLED=true\r\n", [], True),
    ("E8 duplicate: PAPER then LIVE (last wins)", "EXECUTION_MODE=DRY_RUN\nEXECUTION_MODE=LIVE\n", [], True),
    ("E9 duplicate: LIVE then DRY_RUN (last wins -> safe)", "EXECUTION_MODE=LIVE\nEXECUTION_MODE=DRY_RUN\n", [], False),
    ("E10 commented-out live line only", "#EXECUTION_MODE=LIVE\nEXECUTION_MODE=DRY_RUN\n", [], False),
    ("E11 creds in .env", f"BITGET_API_KEY={CANARY}\n", [], True),
    ("E12 blank quoted creds", 'BITGET_API_KEY=""\nBITGET_API_SECRET=\n', [], False),
    ("E13 EXECUTION_ENABLED=1", "EXECUTION_ENABLED=1\n", [], True),
    ("E14 EXECUTION_ENABLED=on", "EXECUTION_ENABLED=on\n", [], True),
    ("E15 EXECUTION_ENABLED=t (pydantic accepts 't')  [BYPASS 3]", "EXECUTION_ENABLED=t\n", [], True),
    ("E16 interpolation EXECUTION_MODE=${X}  [BYPASS 2]", "X=LIVE\nEXECUTION_MODE=${X}\n", [], True),
    ("F .env LIVE, OS DRY_RUN (the app runs DRY_RUN)", "EXECUTION_MODE=LIVE\n", [("EXECUTION_MODE", "DRY_RUN")], None),
    ("G1 execution_mode=LIVE then EXECUTION_MODE=DRY_RUN  [BYPASS 1]", PAPER,
     [("execution_mode", "LIVE"), ("EXECUTION_MODE", "DRY_RUN")], None),
    ("G2 EXECUTION_MODE=DRY_RUN then execution_mode=LIVE  [BYPASS 1]", PAPER,
     [("EXECUTION_MODE", "DRY_RUN"), ("execution_mode", "LIVE")], None),
    ("G3 BITGET_API_KEY= (blank) then bitget_api_key=CANARY", PAPER,
     [("BITGET_API_KEY", ""), ("bitget_api_key", CANARY)], None),
    ("G4 bitget_api_key=CANARY then BITGET_API_KEY= (blank)", PAPER,
     [("bitget_api_key", CANARY), ("BITGET_API_KEY", "")], None),
]
assert len(CASES) == 31          # A..G; the five H (running-engine) cases are below


@pytest.mark.parametrize("name, env_text, os_env, expect_refuse", CASES, ids=[c[0].split()[0] for c in CASES])
def test_the_launcher_decides_exactly_as_the_app_resolves(tmp_path, name, env_text, os_env, expect_refuse):
    result, calls, proj, truth = run_start_bot(tmp_path, env_text=env_text, env_order=os_env, with_truth=True)
    was_refused = refused(result, calls, proj)
    app_says = live_capable(truth)
    if app_says is None:                                   # the app itself cannot resolve it -> the launcher must refuse
        assert was_refused, (name, result.stderr)
    else:
        assert was_refused == app_says, (name, truth, result.stderr)
    if expect_refuse is not None:
        assert was_refused == expect_refuse, (name, result.stderr)
    assert CANARY not in result.stdout + result.stderr
    if not was_refused:
        assert "nohup" in calls and result.returncode != 90


# ------------------------------------------------------------------ H: a running engine
@pytest.mark.parametrize("name, state, expect_refuse", [
    ("H1 state mode=LIVE pid=<alive>", "mode=LIVE\npid={alive}\n", True),
    ("H2 state mode=LIVE pid=<dead 999999>", "mode=LIVE\npid=999999\n", False),
    ("H3 state mode=live (lowercase) pid=<alive>", "mode=live\npid={alive}\n", True),
    ("H4 state mode=FORWARD pid=<alive>", "mode=FORWARD\npid={alive}\n", False),
], ids=["H1", "H2", "H3", "H4"])
def test_a_running_live_engine_recorded_in_the_state_file(tmp_path, name, state, expect_refuse):
    result, calls, proj = run_start_bot(tmp_path, live_state=state.format(alive=os.getpid()))
    assert (result.returncode == 90) == expect_refuse, (name, result.stderr)
    assert ("nohup" in calls) == (not expect_refuse)


def test_h5_a_live_engine_with_no_state_file_is_left_alone_because_it_lives_in_another_directory(tmp_path):
    """Agent C: with no state file, `pkill -9 app.main` would still have killed an engine the ceremony did not record.
    The kill is now scoped to this project's directory, so an engine anywhere else survives the paper launcher."""
    other = tmp_path / "another_checkout"
    other.mkdir()
    (tmp_path / "proj").mkdir(exist_ok=True)
    engine = spawn_decoy(other)
    try:
        result, calls, _proj = run_start_bot(tmp_path)
        assert result.returncode != 90 and "nohup" in calls
        assert engine.poll() is None
    finally:
        engine.kill()
        engine.wait()
