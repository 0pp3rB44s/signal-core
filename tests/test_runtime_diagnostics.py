from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime_diagnostics import RuntimeDiagnostics


def test_detached_launcher_survives_launcher_parent_exit(tmp_path):
    output = tmp_path / "child.out"
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_detached.py"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "--stdout",
            str(output),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    pid = int(result.stdout.strip())
    try:
        os.kill(pid, 0)
        assert os.getpgid(pid) == pid
    finally:
        os.kill(pid, signal.SIGTERM)


def test_runtime_heartbeat_tracks_completed_cycles(tmp_path):
    diagnostics = RuntimeDiagnostics(
        tmp_path / "heartbeat.json",
        tmp_path / "shutdown.json",
    )
    diagnostics._installed = True
    diagnostics.heartbeat("scan_cycle_start", scan_started=True)
    diagnostics.heartbeat(
        "scan_cycle_complete",
        scan_completed=True,
        plan_count=2,
    )

    payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert payload["stage"] == "scan_cycle_complete"
    assert payload["scan_cycles_started"] == 1
    assert payload["scan_cycles_completed"] == 1
    assert payload["details"]["plan_count"] == 2
    assert payload["pid"] == os.getpid()


def test_signal_reason_and_exit_code_are_persisted(tmp_path):
    diagnostics = RuntimeDiagnostics(
        tmp_path / "heartbeat.json",
        tmp_path / "shutdown.json",
    )
    diagnostics._installed = True

    with pytest.raises(SystemExit) as exc:
        diagnostics._signal_handler(signal.SIGTERM, None)

    assert exc.value.code == 128 + signal.SIGTERM
    payload = json.loads((tmp_path / "shutdown.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "signal:SIGTERM"
    assert payload["signal"] == "SIGTERM"
    assert payload["exit_code"] == 128 + signal.SIGTERM


def test_uninstalled_runtime_does_not_create_files(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    diagnostics = RuntimeDiagnostics(heartbeat, tmp_path / "shutdown.json")
    diagnostics.heartbeat("test")
    assert not heartbeat.exists()
