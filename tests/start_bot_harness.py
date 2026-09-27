"""Hermetic harness: the REAL scripts/start_bot.sh + scripts/lib/env_guard.sh + the REAL app settings resolver in a
temporary project. `nohup` and `sleep` are shims (so nothing is ever started); process listing/killing is REAL, aimed
only at decoy `sleep` processes this harness spawns itself."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PAPER = "EXECUTION_MODE=DRY_RUN\nEXECUTION_ENABLED=false\n"


def make_project(tmp_path: Path, env_text: str | bytes | None = PAPER):
    proj = tmp_path / "proj"
    (proj / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "start_bot.sh", proj / "scripts" / "start_bot.sh")
    shutil.copy(REPO / "scripts" / "lib" / "env_guard.sh", proj / "scripts" / "lib" / "env_guard.sh")
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / ".venv" / "bin" / "activate").write_text("")
    (proj / ".venv" / "bin" / "python3").symlink_to(sys.executable)          # the app's own interpreter
    if env_text is not None:
        (proj / ".env").write_bytes(env_text if isinstance(env_text, bytes) else env_text.encode())
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    log = tmp_path / "shim_calls.log"
    for name in ("nohup", "sleep"):
        shim = shims / name
        shim.write_text(f"#!/bin/bash\necho \"{name} $*\" >> {log}\nexit 0\n")
        shim.chmod(0o755)
    return proj, shims, log


def spawn_decoy(cwd: Path) -> subprocess.Popen:
    """A harmless process whose command line names app.main and whose cwd is `cwd`."""
    proc = subprocess.Popen(["/bin/bash", "-c", "exec -a app.main /bin/sleep 120"], cwd=cwd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:                       # wait until exec has renamed it
        out = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True).stdout
        if "app.main" in out:
            break
        time.sleep(0.02)
    return proc


def _python_path() -> str:
    """The project (so `import app` works from the temporary project) plus the interpreter's own site-packages
    (the hermetic project's `.venv/bin/python3` is only a symlink to it, so it does not find them by itself)."""
    extra = [p for p in sys.path if "site-packages" in p]
    return os.pathsep.join([str(REPO), *extra])


def launch_env(tmp_path: Path, shims: Path, exported=None, env_order=None) -> dict[str, str]:
    env = {"PATH": f"{shims}:{BASE_PATH}", "HOME": str(tmp_path), "PYTHONPATH": _python_path()}
    if env_order is not None:
        env.update(dict(env_order))                     # insertion order is preserved by subprocess
    env.update(exported or {})
    return env


def settings_truth(proj: Path, env: dict[str, str]) -> dict:
    """What the APP resolves for this exact environment. Resolved THROUGH bash, because start_bot.sh launches
    app.main from bash and the app therefore sees bash's re-exported environment (order included)."""
    code = ("import json;from app.config import Settings;s=Settings();"
            "print(json.dumps(dict(mode=s.execution_mode,enabled=s.execution_enabled,live=s.is_live_execution,"
            "key=bool(s.bitget_api_key.get_secret_value()),sec=bool(s.bitget_api_secret.get_secret_value()),"
            "pp=bool(s.bitget_api_passphrase.get_secret_value()))))")
    cp = subprocess.run(["/bin/bash", "-c", 'exec "$0" -c "$1"', sys.executable, code], cwd=proj, env=env,
                        capture_output=True, text=True, timeout=60)
    try:
        import json
        return json.loads(cp.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (cp.stderr or cp.stdout)[-200:]}


def live_capable(truth: dict) -> bool | None:
    if "error" in truth:
        return None
    return bool(str(truth["mode"]).strip().upper() == "LIVE" or truth["enabled"] or truth["live"]
                or truth["key"] or truth["sec"] or truth["pp"])


def run_start_bot(tmp_path: Path, *, env_text=PAPER, exported=None, live_state: str | None = None,
                  env_order: list[tuple[str, str]] | None = None, with_truth: bool = False):
    proj, shims, log = make_project(tmp_path, env_text)
    if live_state is not None:
        (proj / "state").mkdir()
        (proj / "state" / "live_runtime.state").write_text(live_state)
    env = launch_env(tmp_path, shims, exported, env_order)
    result = subprocess.run(["/bin/bash", str(proj / "scripts" / "start_bot.sh")], cwd=proj, env=env,
                            capture_output=True, text=True, timeout=60)
    calls = log.read_text() if log.exists() else ""
    if result.returncode != 90:                         # the shimmed `nohup` is a background job: let it land
        deadline = time.time() + 3
        while "nohup" not in calls and time.time() < deadline:
            time.sleep(0.05)
            calls = log.read_text() if log.exists() else ""
    if with_truth:
        return result, calls, proj, settings_truth(proj, env)
    return result, calls, proj


def refused(result, calls, proj) -> bool:
    return (result.returncode == 90 and "nohup" not in calls
            and not (proj / "state" / "bot.pid").exists() and not (proj / "logs").exists())
