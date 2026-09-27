"""Hardened LIVE watchdog: mode awareness, deduplication, recovery, isolation.

Every test runs against an isolated WD_STATE_DIR fixture. The real
state/runtime_heartbeat.json and the running engine are never touched, and no
test performs an exchange call or a real alert delivery.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCHDOG = REPO / "scripts" / "watchdog_live.sh"
STATE_TOOL = REPO / "scripts" / "lib" / "wd_alert_state.py"


def run_watchdog(state_dir: Path, *args: str, env_extra: dict | None = None):
    env = dict(os.environ)
    env["WD_STATE_DIR"] = str(state_dir)
    env["WD_HEARTBEAT"] = str(state_dir / "runtime_heartbeat.json")
    env.setdefault("WD_COOLDOWN_SEC", "1800")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(WATCHDOG), *args],
        cwd=str(REPO), capture_output=True, text=True, env=env, timeout=120,
    )


def write_heartbeat(state_dir: Path, *, age: float = 0, schema: int = 2,
                    mode: str = "LIVE", stage: str = "scan_cycle_complete", **extra):
    state_dir.mkdir(parents=True, exist_ok=True)
    hb = state_dir / "runtime_heartbeat.json"
    payload = {"schema_version": schema, "mode": mode, "stage": stage,
               "scan_cycles_started": 10, "scan_cycles_completed": 10,
               "last_successful_scan_utc": "2026-07-29T12:00:00Z",
               "last_error_utc": None, "last_error_type": None, **extra}
    hb.write_text(json.dumps(payload), encoding="utf-8")
    if age:
        old = time.time() - age
        os.utime(hb, (old, old))
    return hb


def alert_state(state_dir: Path) -> dict:
    p = state_dir / "watchdog_alerts.json"
    return json.loads(p.read_text()) if p.exists() else {}


def state_tool(state_dir: Path, *args: str):
    return subprocess.run(
        ["/usr/bin/python3", str(STATE_TOOL), *args],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )


# --- syntax / shape ------------------------------------------------------

def test_watchdog_shell_syntax_is_valid():
    assert subprocess.run(["bash", "-n", str(WATCHDOG)]).returncode == 0


def test_watchdog_never_imports_execution_or_mutates_orders():
    src = WATCHDOG.read_text()
    for banned in ("execution_service", "entry_submitter", "place_futures",
                   "close_futures", "cancel_futures", "start_bot.sh",
                   "launch_live.sh", "stop_all.sh", "launchctl load",
                   "launchctl bootstrap", "launchctl kickstart"):
        assert banned not in src, f"watchdog references {banned}"


def test_watchdog_sends_no_signals_to_any_process():
    """`kill -0` is a liveness probe and is allowed; sending a real signal is not."""
    import re
    src = WATCHDOG.read_text()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "pkill" not in stripped, f"pkill present: {stripped}"
        for match in re.finditer(r"\bkill\b\s+(-\S+)?", stripped):
            flag = (match.group(1) or "").strip()
            assert flag == "-0", f"non-probe kill in: {stripped}"


def test_watchdog_makes_no_exchange_calls():
    src = WATCHDOG.read_text()
    assert "api.bitget.com" not in src
    # The only network call permitted is a loopback dashboard probe.
    for line in src.splitlines():
        if "curl" in line and not line.strip().startswith("#"):
            assert "127.0.0.1" in line, f"non-loopback curl: {line.strip()}"
            assert "-m 5" in line, "curl must carry a strict timeout"


# --- 1. mode awareness: no forward-paper checks in LIVE ------------------

def test_live_mode_runs_no_forward_paper_checks(tmp_path):
    write_heartbeat(tmp_path, age=0, mode="LIVE")
    result = run_watchdog(tmp_path, "--dry-run")
    out = result.stdout
    assert "mode=LIVE" in out
    for noise in ("forward_paper_keepalive", "MONITOR_DEAD",
                  "validation_72h", "check_forward_paper"):
        assert noise not in out, f"forward-paper check leaked into LIVE: {noise}"


def test_mode_comes_from_a_fresh_heartbeat(tmp_path):
    write_heartbeat(tmp_path, age=0, mode="DRY_RUN")
    assert "mode=DRY_RUN (via heartbeat)" in run_watchdog(tmp_path, "--dry-run").stdout


def test_stale_heartbeat_mode_is_not_trusted(tmp_path):
    """A stale heartbeat's mode is ignored; fall back to config, not to LIVE."""
    write_heartbeat(tmp_path, age=99999, mode="DRY_RUN")
    out = run_watchdog(tmp_path, "--dry-run").stdout
    assert "via heartbeat" not in out


def test_unknown_mode_skips_supervisor_check(tmp_path, monkeypatch):
    write_heartbeat(tmp_path, age=99999, mode="")
    out = run_watchdog(tmp_path, "--dry-run", env_extra={"WD_FORCE_NO_ENV": "1"}).stdout
    assert "mode=" in out


# --- 2. dedup: first sends, repeat suppressed ---------------------------

def test_first_alert_sends_and_repeat_is_deduplicated(tmp_path):
    state = tmp_path / "watchdog_alerts.json"
    now = int(time.time())
    r1 = state_tool(tmp_path, "raise", "--state", str(state), "--key", "TEST_KEY",
                    "--severity", "CRITICAL", "--message", "first",
                    "--now", str(now), "--cooldown", "1800", "--no-deliver", "1")
    assert "suppressed by --no-deliver" in r1.stdout

    # Simulate a delivered first alert so the cooldown path is exercised.
    data = json.loads(state.read_text())
    data["TEST_KEY"]["delivered"] = True
    data["TEST_KEY"]["last_delivered"] = now
    state.write_text(json.dumps(data))

    r2 = state_tool(tmp_path, "raise", "--state", str(state), "--key", "TEST_KEY",
                    "--severity", "CRITICAL", "--message", "second",
                    "--now", str(now + 60), "--cooldown", "1800", "--no-deliver", "1")
    assert "deduplicated" in r2.stdout
    assert json.loads(state.read_text())["TEST_KEY"]["count"] == 2


def test_cooldown_expiry_allows_a_new_send(tmp_path):
    state = tmp_path / "a.json"
    now = int(time.time())
    state.write_text(json.dumps({"K": {
        "key": "K", "severity": "HIGH", "message": "m", "first_seen": now,
        "last_seen": now, "count": 1, "last_delivered": now, "delivered": True,
        "resolved_sent": False}}))
    out = state_tool(tmp_path, "raise", "--state", str(state), "--key", "K",
                     "--severity", "HIGH", "--message", "m",
                     "--now", str(now + 1801), "--cooldown", "1800",
                     "--no-deliver", "1").stdout
    assert "cooldown elapsed" in out


def test_severity_escalation_bypasses_cooldown(tmp_path):
    state = tmp_path / "a.json"
    now = int(time.time())
    state.write_text(json.dumps({"K": {
        "key": "K", "severity": "HIGH", "message": "m", "first_seen": now,
        "last_seen": now, "count": 1, "last_delivered": now, "delivered": True,
        "resolved_sent": False}}))
    out = state_tool(tmp_path, "raise", "--state", str(state), "--key", "K",
                     "--severity", "CRITICAL", "--message", "worse",
                     "--now", str(now + 10), "--cooldown", "1800",
                     "--no-deliver", "1").stdout
    assert "severity escalation" in out


# --- 3. recovery --------------------------------------------------------

def test_resolved_is_sent_once_for_a_delivered_incident(tmp_path):
    state = tmp_path / "a.json"
    now = int(time.time())
    state.write_text(json.dumps({"K": {
        "key": "K", "severity": "CRITICAL", "message": "m",
        "first_seen": now - 600, "last_seen": now - 60, "count": 5,
        "last_delivered": now - 600, "delivered": True, "resolved_sent": False}}))

    out = state_tool(tmp_path, "resolve", "--state", str(state),
                     "--now", str(now), "--active", "", "--no-deliver", "1").stdout
    assert "resolved K" in out
    assert alert_state_file(state) == {}, "entry must be cleared after resolve"

    out2 = state_tool(tmp_path, "resolve", "--state", str(state),
                      "--now", str(now + 60), "--active", "", "--no-deliver", "1").stdout
    assert "resolved K" not in out2, "RESOLVED must be emitted exactly once"


def alert_state_file(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def test_no_resolved_for_an_undelivered_incident(tmp_path):
    """An incident nobody was told about must not produce a phantom all-clear."""
    state = tmp_path / "a.json"
    now = int(time.time())
    state.write_text(json.dumps({"K": {
        "key": "K", "severity": "HIGH", "message": "m", "first_seen": now - 100,
        "last_seen": now - 10, "count": 3, "last_delivered": None,
        "delivered": False, "resolved_sent": False}}))
    out = state_tool(tmp_path, "resolve", "--state", str(state),
                     "--now", str(now), "--active", "", "--no-deliver", "1").stdout
    assert "resolved K" not in out
    assert alert_state_file(state) == {}


def test_still_active_key_is_not_resolved(tmp_path):
    state = tmp_path / "a.json"
    now = int(time.time())
    state.write_text(json.dumps({"K": {
        "key": "K", "severity": "HIGH", "message": "m", "first_seen": now,
        "last_seen": now, "count": 1, "last_delivered": now, "delivered": True,
        "resolved_sent": False}}))
    state_tool(tmp_path, "resolve", "--state", str(state), "--now", str(now),
               "--active", "K", "--no-deliver", "1")
    assert "K" in alert_state_file(state)


# --- 4. delivery failure ------------------------------------------------

def test_failed_delivery_is_not_marked_sent(tmp_path):
    """With no provider configured, alert.sh returns 3 and nothing is 'sent'."""
    state = tmp_path / "a.json"
    now = int(time.time())
    state_tool(tmp_path, "raise", "--state", str(state), "--key", "K",
               "--severity", "CRITICAL", "--message", "m", "--now", str(now),
               "--cooldown", "1800", "--no-deliver", "0")
    entry = alert_state_file(state)["K"]
    assert entry["delivered"] is False
    assert entry["last_delivered"] is None


def test_corrupt_alert_state_is_quarantined_not_fatal(tmp_path):
    state = tmp_path / "a.json"
    state.write_text("{ this is not json")
    now = int(time.time())
    result = state_tool(tmp_path, "raise", "--state", str(state), "--key", "K",
                        "--severity", "HIGH", "--message", "m", "--now", str(now),
                        "--cooldown", "1800", "--no-deliver", "1")
    assert result.returncode == 0
    assert "K" in alert_state_file(state)
    assert list(tmp_path.glob("a.json.corrupt_*")), "corrupt state must be quarantined"


def test_state_write_is_atomic(tmp_path):
    state = tmp_path / "a.json"
    now = int(time.time())
    for i in range(15):
        state_tool(tmp_path, "raise", "--state", str(state), "--key", f"K{i}",
                   "--severity", "HIGH", "--message", "m", "--now", str(now),
                   "--cooldown", "1800", "--no-deliver", "1")
    assert not list(tmp_path.glob(".a.json.*")), "temp files leaked"
    assert len(alert_state_file(state)) == 15


# --- 5. concurrency -----------------------------------------------------

def test_duplicate_watchdog_exits_cleanly_without_evaluating(tmp_path):
    write_heartbeat(tmp_path, age=0)
    lockdir = tmp_path / "watchdog_live.lock.d"
    lockdir.mkdir(parents=True, exist_ok=True)   # simulate a run in progress
    try:
        result = run_watchdog(tmp_path, "--dry-run")
        assert result.returncode == 0
        assert "already running" in result.stdout
        assert "[OK]" not in result.stdout, "second instance must not evaluate checks"
    finally:
        lockdir.rmdir()


def test_stale_lock_is_reclaimed(tmp_path):
    write_heartbeat(tmp_path, age=0)
    lockdir = tmp_path / "watchdog_live.lock.d"
    lockdir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 1200          # older than the 600s reclaim threshold
    os.utime(lockdir, (old, old))
    result = run_watchdog(tmp_path, "--dry-run")
    assert "reclaiming stale lock" in result.stdout


# --- 6. genuine LIVE detections -----------------------------------------

def test_stale_heartbeat_with_live_engine_raises_not_advancing(tmp_path):
    write_heartbeat(tmp_path, age=99999)
    (tmp_path / "bot.pid").write_text(str(os.getpid()))  # a real, live pid
    out = run_watchdog(tmp_path, "--dry-run").stdout
    assert "heartbeat STALE" in out
    # This process is not app.main, so identity is flagged too - that is correct.
    assert "would alert" in out


def test_missing_pid_file_is_detected(tmp_path):
    write_heartbeat(tmp_path, age=0)
    out = run_watchdog(tmp_path, "--dry-run").stdout
    assert "bot.pid missing" in out
    assert "BOT_PID_MISSING" in out


def test_recycled_pid_is_detected(tmp_path):
    write_heartbeat(tmp_path, age=0)
    (tmp_path / "bot.pid").write_text(str(os.getpid()))
    out = run_watchdog(tmp_path, "--dry-run").stdout
    assert "not the engine" in out
    assert "BOT_PID_WRONG_COMMAND" in out


def test_dry_run_writes_no_state(tmp_path):
    write_heartbeat(tmp_path, age=0)
    run_watchdog(tmp_path, "--dry-run")
    assert not (tmp_path / "watchdog_alerts.json").exists()
    assert not (tmp_path / "watchdog_live_heartbeat.json").exists()


def test_normal_run_writes_its_own_heartbeat(tmp_path):
    write_heartbeat(tmp_path, age=0)
    run_watchdog(tmp_path, "--no-deliver")
    hb = json.loads((tmp_path / "watchdog_live_heartbeat.json").read_text())
    assert hb["mode"] in {"LIVE", "DRY_RUN", "UNKNOWN", "FORWARD_PAPER"}
    assert "findings" in hb


# --- 7. secret protection -----------------------------------------------

def test_no_secret_material_in_watchdog_or_state_tool():
    for path in (WATCHDOG, STATE_TOOL):
        src = path.read_text()
        for token in ("DISCORD_WEBHOOK_URL=", "TELEGRAM_BOT_TOKEN=",
                      "discord.com/api/webhooks", "hooks.slack.com"):
            assert token not in src


def test_state_tool_never_captures_provider_response_bodies():
    src = STATE_TOOL.read_text()
    assert ">/dev/null 2>&1" in src, "alert.sh output must be discarded"
    # Only the exit status may be consulted.
    assert "proc.stdout" not in src and "proc.stderr" not in src


# --- launchctl race regression ------------------------------------------

def test_supervisor_check_uses_print_not_list():
    """`launchctl list` intermittently omits a job that exited 0 and is idle.
    It produced a false SUPERVISOR_MISSING during scheduler validation."""
    src = WATCHDOG.read_text()
    block = src.split("# 9. supervisor")[1].split("# 10.")[0]
    code = [ln for ln in block.splitlines() if not ln.strip().startswith("#")]
    assert any("launchctl print" in ln for ln in code)
    assert not any("launchctl list" in ln for ln in code), \
        "supervisor check must not use the racy `launchctl list`"
