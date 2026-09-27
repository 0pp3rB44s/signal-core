"""The dashboard deploy script must be incapable of touching trading."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_dashboard.sh"


def _run(env_extra: dict, args=("abc123",), tmp_home=None):
    import os
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    if tmp_home:
        env["HOME"] = str(tmp_home)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def _trading(tmp_path: Path) -> Path:
    t = tmp_path / "bitget_ai_agent_phase7"
    (t / "state").mkdir(parents=True)
    return t


def test_script_exists_and_parses():
    assert SCRIPT.exists()
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_aborts_when_pointed_at_the_trading_checkout(tmp_path):
    """The single most important guard: same path must abort."""
    t = _trading(tmp_path)
    r = _run({"CGC_TRADING_ROOT": t, "CGC_DASHBOARD_ROOT": t}, tmp_home=tmp_path)
    assert r.returncode != 0
    assert "equals the trading checkout" in (r.stderr + r.stdout)


def test_aborts_when_dashboard_path_is_inside_the_trading_checkout(tmp_path):
    t = _trading(tmp_path)
    inside = t / "dashboard_runtime"
    inside.mkdir(parents=True)
    r = _run({"CGC_TRADING_ROOT": t, "CGC_DASHBOARD_ROOT": inside}, tmp_home=tmp_path)
    assert r.returncode != 0
    assert "inside the trading checkout" in (r.stderr + r.stdout)


def test_requires_a_sha(tmp_path):
    t = _trading(tmp_path)
    r = _run({"CGC_TRADING_ROOT": t, "CGC_DASHBOARD_ROOT": tmp_path / "dash"},
             args=(), tmp_home=tmp_path)
    assert r.returncode != 0 and "usage" in (r.stderr + r.stdout)


def test_aborts_when_trading_root_has_no_state(tmp_path):
    """Refuses to run against something that is not the production runtime."""
    t = tmp_path / "not_trading"; t.mkdir()
    d = tmp_path / "dash"; d.mkdir()
    r = _run({"CGC_TRADING_ROOT": t, "CGC_DASHBOARD_ROOT": d}, tmp_home=tmp_path)
    assert r.returncode != 0 and "no state/" in (r.stderr + r.stdout)


def test_aborts_when_dashboard_runtime_is_not_a_checkout(tmp_path):
    t = _trading(tmp_path)
    d = tmp_path / "dash"; d.mkdir()
    r = _run({"CGC_TRADING_ROOT": t, "CGC_DASHBOARD_ROOT": d}, tmp_home=tmp_path)
    assert r.returncode != 0 and "not a git checkout" in (r.stderr + r.stdout)


@pytest.mark.parametrize("forbidden", ["launch_live", "stop_all", "com.cgc.live", "app.main "])
def test_script_never_references_engine_control(forbidden):
    """No path through this script may start, stop or signal the executor."""
    body = SCRIPT.read_text()
    if forbidden == "app.main ":
        # app.main appears only inside a read-only pgrep used to compare PIDs.
        for line in body.splitlines():
            if "app\\.main" in line:
                assert "pgrep" in line, f"app.main used outside a read-only pgrep: {line.strip()}"
        return
    assert forbidden not in body, f"deploy script references {forbidden}"


def test_script_reloads_only_the_dashboard_label():
    body = SCRIPT.read_text()
    labels = {ln.split("gui/$UID_N/")[1].split()[0].strip('"')
              for ln in body.splitlines() if "gui/$UID_N/" in ln and "bootout" in ln}
    assert labels <= {"$LABEL"}, f"unexpected launchctl targets: {labels}"
    assert 'LABEL="com.cgc.dashboard"' in body


def test_script_verifies_engine_pid_is_unchanged():
    body = SCRIPT.read_text()
    assert "ENGINE_BEFORE" in body and "ENGINE_AFTER" in body
    assert 'ENGINE_BEFORE" = "$ENGINE_AFTER' in body


def test_script_rolls_back_on_failed_healthcheck():
    body = SCRIPT.read_text()
    assert "rolling dashboard back" in body
    assert 'checkout -q --detach "$PREV"' in body
