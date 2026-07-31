"""RETIRED CONTROL SURFACE — observation only.

dashboard_v2 is retained solely as a rollback target for dashboard_v3. Its
process-control capability has been removed, deliberately and permanently.

Why: start_bot() shelled out to scripts/start_bot.sh, which bypasses all four
authorisation layers in scripts/launch_live.sh (open critical risks, the
owner-signed LIVE_PILOT_AUTHORISATION token, the .env.live invariants, and the
typed confirmation). One HTTP POST could therefore begin real-money trading, and
stop_bot() could kill an engine holding an open position.

Starting and stopping the engine is an operator action performed from a terminal
through the launcher that enforces those layers. It is not a web endpoint.
is_bot_running() is kept because it only observes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parents[1]
BOT_PID_PATH = BASE_PATH / "state" / "bot.pid"

#: Returned by the retired entry points so any surviving caller fails loudly and
#: safely rather than silently appearing to succeed.
_RETIRED: dict[str, Any] = {
    "ok": False,
    "running": None,
    "pid": None,
    "message": (
        "Process control has been removed from the dashboard. Start the engine "
        "with scripts/launch_live.sh (four authorisation layers) and stop it "
        "with scripts/stop_all.sh, from a terminal."
    ),
}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def is_bot_running() -> tuple[bool, int | None]:
    """Observation only: state/bot.pid liveness, falling back to pgrep."""
    if BOT_PID_PATH.exists():
        try:
            pid = int(BOT_PID_PATH.read_text().strip())
        except ValueError:
            pid = None
        if pid and _pid_alive(pid):
            return True, pid
    try:
        result = subprocess.run(
            ["pgrep", "-f", "app.main"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False, None
    if result.returncode == 0 and result.stdout.strip():
        try:
            return True, int(result.stdout.strip().splitlines()[0])
        except ValueError:
            return True, None
    return False, None


def start_bot(reason: str = "") -> dict[str, Any]:
    """RETIRED. Never starts a process."""
    return dict(_RETIRED)


def stop_bot(reason: str = "") -> dict[str, Any]:
    """RETIRED. Never stops a process."""
    return dict(_RETIRED)
