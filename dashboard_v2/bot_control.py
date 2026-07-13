"""Real bot process control: status check + start/stop, backed by the same
scripts already used manually (scripts/start_bot.sh, scripts/stop_all.sh) and
the same process-check logic as scripts/healthcheck.sh."""

import os
import subprocess
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parents[1]
BOT_PID_PATH = BASE_PATH / "state" / "bot.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def is_bot_running() -> tuple[bool, int | None]:
    """Mirrors scripts/healthcheck.sh: state/bot.pid + liveness check, falling
    back to pgrep -f app.main (covers manual foreground runs with no pid file)."""
    if BOT_PID_PATH.exists():
        try:
            pid = int(BOT_PID_PATH.read_text().strip())
        except ValueError:
            pid = None
        if pid and _pid_alive(pid):
            return True, pid

    try:
        result = subprocess.run(
            ["pgrep", "-f", "app.main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False, None

    if result.returncode == 0 and result.stdout.strip():
        try:
            pid = int(result.stdout.strip().splitlines()[0])
            return True, pid
        except ValueError:
            return True, None

    return False, None


def start_bot(reason: str = "dashboard_start") -> dict[str, Any]:
    running, pid = is_bot_running()
    if running:
        return {"ok": True, "message": f"Bot already running (pid {pid}).", "running": True, "pid": pid}

    try:
        result = subprocess.run(
            ["scripts/start_bot.sh", reason],
            cwd=str(BASE_PATH),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "message": f"Failed to start bot: {exc}", "running": False, "pid": None}

    running, pid = is_bot_running()
    message = (result.stdout or result.stderr or "").strip()[-500:]
    return {"ok": result.returncode == 0 and running, "message": message or "Start command completed.", "running": running, "pid": pid}


def stop_bot(reason: str = "dashboard_stop") -> dict[str, Any]:
    running, _pid = is_bot_running()
    if not running:
        return {"ok": True, "message": "Bot already stopped.", "running": False, "pid": None}

    try:
        result = subprocess.run(
            ["scripts/stop_all.sh", reason],
            cwd=str(BASE_PATH),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "message": f"Failed to stop bot: {exc}", "running": True, "pid": _pid}

    running, pid = is_bot_running()
    message = (result.stdout or result.stderr or "").strip()[-500:]
    return {"ok": result.returncode == 0 and not running, "message": message or "Stop command completed.", "running": running, "pid": pid}
