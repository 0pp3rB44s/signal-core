"""Heartbeat liveness contract.

Regression origin: `runtime_heartbeat("scan_cycle_start"/"scan_cycle_complete")`
was gated behind `settings.forward_paper_only` (app/runner.py:615 and :1503), so
in LIVE mode the heartbeat froze at `process_started` forever. On 2026-07-29 the
host slept for 3 h with the engine suspended and nothing detected it, because the
watchdog's only liveness signal never advanced.

These tests pin the contract: the heartbeat advances in every runtime mode, a
failed cycle never looks like a successful one, and no observability failure can
propagate into the trading loop.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.runtime_diagnostics import RuntimeDiagnostics


def _diag(tmp_path: Path) -> RuntimeDiagnostics:
    """A diagnostics instance that writes to tmp and is 'installed' without
    touching real signal handlers or atexit."""
    d = RuntimeDiagnostics(
        heartbeat_path=tmp_path / "runtime_heartbeat.json",
        shutdown_path=tmp_path / "last_shutdown.json",
    )
    d._installed = True
    return d


def _read(d: RuntimeDiagnostics) -> dict:
    return json.loads(Path(d.heartbeat_path).read_text(encoding="utf-8"))


# --- 1 / 2. every mode emits, and counters advance -----------------------


@pytest.mark.parametrize("mode", ["LIVE", "DRY_RUN", "FORWARD_PAPER"])
def test_heartbeat_advances_in_every_runtime_mode(tmp_path, mode):
    d = _diag(tmp_path)
    d.set_runtime_mode(mode)

    d.heartbeat("scan_cycle_start", scan_started=True)
    started = _read(d)
    assert started["stage"] == "scan_cycle_start"
    assert started["mode"] == mode
    assert started["scan_cycles_started"] == 1
    assert started["scan_cycles_completed"] == 0
    assert started["last_successful_scan_utc"] is None

    d.heartbeat("scan_cycle_complete", scan_completed=True, plan_count=3)
    done = _read(d)
    assert done["stage"] == "scan_cycle_complete"
    assert done["scan_cycles_started"] == 1
    assert done["scan_cycles_completed"] == 1
    assert done["last_successful_scan_utc"] is not None
    assert done["details"]["plan_count"] == 3


def test_required_fields_are_present(tmp_path):
    d = _diag(tmp_path)
    d.set_runtime_mode("LIVE")
    d.heartbeat("scan_cycle_start", scan_started=True)
    payload = _read(d)
    for field in (
        "pid", "mode", "stage", "timestamp", "commit",
        "scan_cycles_started", "scan_cycles_completed",
        "last_successful_scan_utc", "last_error_utc", "last_error_type",
    ):
        assert field in payload, f"missing required heartbeat field: {field}"
    assert payload["pid"] == os.getpid()


# --- 3. a failed cycle must not look successful -------------------------


def test_failed_cycle_records_error_and_does_not_advance_completed(tmp_path):
    d = _diag(tmp_path)
    d.set_runtime_mode("LIVE")

    d.heartbeat("scan_cycle_start", scan_started=True)
    d.heartbeat("scan_cycle_failed", error_type="ReadTimeout", consecutive_scan_failures=1)

    failed = _read(d)
    assert failed["stage"] == "scan_cycle_failed"
    assert failed["scan_cycles_started"] == 1
    assert failed["scan_cycles_completed"] == 0, "a failed scan must never advance completions"
    assert failed["last_error_type"] == "ReadTimeout"
    assert failed["last_error_utc"] is not None
    assert failed["last_successful_scan_utc"] is None


def test_incomplete_cycle_does_not_advance_completed(tmp_path):
    d = _diag(tmp_path)
    d.heartbeat("scan_cycle_start", scan_started=True)
    d.heartbeat("scan_cycle_incomplete")
    assert _read(d)["scan_cycles_completed"] == 0


def test_last_successful_scan_survives_a_later_failure(tmp_path):
    """After a good cycle then a bad one, the success timestamp is retained so a
    monitor can measure time-since-last-good-scan."""
    d = _diag(tmp_path)
    d.heartbeat("scan_cycle_complete", scan_completed=True)
    good = _read(d)["last_successful_scan_utc"]
    d.heartbeat("scan_cycle_failed", error_type="ConnectionError")
    after = _read(d)
    assert after["last_successful_scan_utc"] == good
    assert after["scan_cycles_completed"] == 1
    assert after["last_error_type"] == "ConnectionError"


# --- 4 / 5 / 6. durability and failure modes ----------------------------


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    d = _diag(tmp_path)
    for i in range(20):
        d.heartbeat("scan_cycle_start", scan_started=True, i=i)
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"atomic write leaked temp files: {leftovers}"
    assert _read(d)["scan_cycles_started"] == 20


def test_corrupt_existing_heartbeat_file_is_overwritten_not_fatal(tmp_path):
    d = _diag(tmp_path)
    Path(d.heartbeat_path).write_text("{ this is not json", encoding="utf-8")

    d.heartbeat("scan_cycle_start", scan_started=True)  # must not raise

    payload = _read(d)
    assert payload["stage"] == "scan_cycle_start"
    assert payload["scan_cycles_started"] == 1


def test_write_permission_failure_is_logged_and_never_raises(tmp_path, caplog):
    d = _diag(tmp_path)
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    d.heartbeat_path = readonly / "runtime_heartbeat.json"
    readonly.chmod(0o500)
    try:
        with caplog.at_level("WARNING"):
            d.heartbeat("scan_cycle_start", scan_started=True)  # must not raise
        assert any("HEARTBEAT_WRITE_FAILED" in r.message for r in caplog.records)
    finally:
        readonly.chmod(0o700)


def test_uninstalled_diagnostics_is_a_no_op(tmp_path):
    d = RuntimeDiagnostics(
        heartbeat_path=tmp_path / "hb.json", shutdown_path=tmp_path / "sd.json"
    )
    d.heartbeat("scan_cycle_start", scan_started=True)
    assert not Path(d.heartbeat_path).exists()


# --- shutdown stages ----------------------------------------------------


def test_shutdown_emits_started_and_completed_stages(tmp_path):
    d = _diag(tmp_path)
    d.set_runtime_mode("LIVE")
    d.record_shutdown("signal:SIGTERM", exit_code=143, signal_name="SIGTERM")

    hb = _read(d)
    assert hb["stage"] == "shutdown_completed"
    shutdown = json.loads(Path(d.shutdown_path).read_text(encoding="utf-8"))
    assert shutdown["reason"] == "signal:SIGTERM"
    assert shutdown["exit_code"] == 143


# --- 8. exactly one writer ----------------------------------------------


def test_single_heartbeat_writer_in_production_code():
    """Only runtime_diagnostics may write the heartbeat file."""
    repo = Path(__file__).resolve().parents[1]
    skip = {"tests", ".claude", "__pycache__", ".venv", ".venv-archiver"}
    writers = []
    for path in repo.rglob("*.py"):
        if skip & set(path.relative_to(repo).parts):
            continue
        text = path.read_text(errors="ignore")
        if "runtime_heartbeat.json" in text and "atomic_write_json" in text:
            writers.append(path.relative_to(repo).as_posix())
    assert writers == ["app/runtime_diagnostics.py"], f"multiple heartbeat writers: {writers}"


# --- 9. the runner no longer gates liveness on forward-paper -------------


def test_runner_does_not_gate_scan_heartbeats_on_forward_paper_only():
    runner = (Path(__file__).resolve().parents[1] / "app" / "runner.py").read_text()
    for stage in ("scan_cycle_start", "scan_cycle_complete", "scan_cycle_incomplete"):
        idx = runner.index(f'"{stage}"')
        window = runner[max(0, idx - 400):idx]
        tail = window.rsplit("\n", 6)[-6:]
        assert not any("forward_paper_only" in line for line in tail), (
            f"{stage} heartbeat still gated behind forward_paper_only"
        )


# --- 7. watchdog compatibility ------------------------------------------


def test_watchdog_detects_fresh_and_stale_heartbeat(tmp_path):
    """The watchdog measures file mtime, so it is schema-agnostic. Prove both
    verdicts against the real shell logic with a controlled mtime."""
    import subprocess
    import time

    hb = tmp_path / "runtime_heartbeat.json"
    hb.write_text('{"stage":"scan_cycle_complete"}', encoding="utf-8")

    # Verbatim copy of scripts/watchdog.sh's age_of() + freshness branch.
    # Built with replace() rather than %-formatting because the shell body
    # itself contains `stat -f %m`.
    template = (
        'now=$(date +%s); '
        'age_of() { [ -f "$1" ] || { echo -1; return; }; echo $(( now - $(stat -f %m "$1") )); }; '
        'HB_AGE=$(age_of "__HB__"); MAX=__MAX__; '
        'if [ "$HB_AGE" -lt 0 ]; then echo MISSING; '
        'elif [ "$HB_AGE" -gt "$MAX" ]; then echo STALE; else echo FRESH; fi'
    )

    def verdict() -> str:
        script = template.replace("__HB__", str(hb)).replace("__MAX__", "600")
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True
        ).stdout.strip()

    assert verdict() == "FRESH"

    old = time.time() - 3600
    os.utime(hb, (old, old))
    assert verdict() == "STALE", "watchdog must flag a heartbeat older than the threshold"

    hb.unlink()
    assert verdict() == "MISSING"


def test_watchdog_reads_mtime_not_schema():
    """Guards the compatibility assumption: if the watchdog ever starts parsing
    the JSON, schema_version bumps become breaking changes."""
    wd = (Path(__file__).resolve().parents[1] / "scripts" / "watchdog.sh").read_text()
    assert "stat -f %m" in wd
    assert "schema_version" not in wd
