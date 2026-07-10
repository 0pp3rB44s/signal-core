from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# The bot has two historical entrypoints (app.main wraps app.runner); both
# patterns must be covered or a restart can leave a second live instance
# trading next to the old one.
BOT_PROCESS_PATTERNS = ("app.main", "app.runner")
BOT_START_SCRIPT = Path("scripts/start_bot.sh")
BOT_START_COMMAND = os.getenv("CGC_BOT_START_COMMAND", "")


@dataclass
class RuntimeResult:
    success: bool
    output: str


def stop_bot() -> RuntimeResult:
    outputs = []
    for pattern in BOT_PROCESS_PATTERNS:
        completed = subprocess.run(
            f"pkill -f '{pattern}' || true",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        outputs.append((completed.stdout + completed.stderr).strip())
    time.sleep(1)
    return RuntimeResult(True, "\n".join(o for o in outputs if o))


def start_bot() -> RuntimeResult:
    Path("logs").mkdir(exist_ok=True)

    if BOT_START_COMMAND:
        cmd = f"nohup {BOT_START_COMMAND} >> logs/bot_runtime.log 2>&1 &"
    elif BOT_START_SCRIPT.exists():
        # Proven start path: venv, .env checks, duplicate-kill and pid
        # bookkeeping all live in this script.
        cmd = f"bash {BOT_START_SCRIPT} cgcagent_restart >> logs/bot_runtime.log 2>&1"
    else:
        return RuntimeResult(False, "No start method: scripts/start_bot.sh missing and CGC_BOT_START_COMMAND not set.")

    completed = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    time.sleep(3)
    return RuntimeResult(completed.returncode == 0, (completed.stdout + completed.stderr).strip())


def bot_running() -> RuntimeResult:
    for pattern in BOT_PROCESS_PATTERNS:
        completed = subprocess.run(
            f"pgrep -fl '{pattern}'",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode == 0:
            return RuntimeResult(True, output)
    return RuntimeResult(False, "")


def restart_bot() -> RuntimeResult:
    stop_bot()
    started = start_bot()
    running = bot_running()

    output = "\n".join([
        "Bot restart requested.",
        f"Start success: {started.success}",
        f"Running: {running.success}",
        running.output,
    ]).strip()

    return RuntimeResult(started.success and running.success, output)
