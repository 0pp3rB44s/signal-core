"""SIGNAL_CORE_FUNNEL_V1 owner DEMO ceremony - DEMO/PAPER ONLY, never real money.

CRITICAL SEPARATION from lsrl.tools.owner_ceremony: that module is LSRL_V1's real-capital MICRO_LIVE
arming ceremony (its Identity/validate() hard-require mode=='MICRO_LIVE', symbol=='SOLUSDT',
strategy=='LSRL_V1' - see lsrl/tools/owner_ceremony.py). It is NEVER imported here, and this module
imports nothing from execution/, lsrl.adapters.live, or lsrl.adapters.authority (LiveAuthority) -
there is no code path in this file that can ever submit a real order. Successfully typing the phrase
below only confirms/observes the already-running, SimExchange-only continuous PAPER/SHADOW drivers
(funnel.tools.continuous_paper_driver); it authorizes nothing beyond that.

The owner is nontechnical: run this file directly (see start_demo.sh at the repo root for the exact
one-line command) and follow the numbered prompts. Every gate is a real check against real state
(the frozen baseline file, the live Bitget public clock endpoint, real disk space, the actual
RiskManager construction, the actual running continuous-driver status files) - nothing here is
decorative.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from clients.clock_safety import CLOCK_OFFSET_LIMIT_MS, probe_exchange_clock
from risk.risk_manager import RiskManager

from funnel.tools.baseline_freeze import REPO_ROOT, verify_baseline_unchanged
from funnel.tools.continuous_paper_driver import DISK_FREE_FLOOR_GB, VARIANTS, _disk_free_gb, _paths

PHRASE = "START LIVE PILOT"
PHRASE_MEANING = "DEMO/PAPER execution only - this can never authorize a real order"
INPUT_TIMEOUT_S = 60.0
TICKET_TTL_S = 10.0


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


def gate_baseline_unchanged() -> GateResult:
    try:
        ok, mismatches = verify_baseline_unchanged()
    except FileNotFoundError:
        return GateResult("baseline_frozen_unchanged", False, "BASELINE_FREEZE_2026-09-27.json not found")
    return GateResult("baseline_frozen_unchanged", ok,
                      "matches pinned freeze" if ok else f"DRIFTED: {mismatches}")


def gate_repo_clean() -> GateResult:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    clean = out.returncode == 0 and not out.stdout.strip()
    return GateResult("repo_clean_tree", clean, "clean" if clean else f"dirty:\n{out.stdout}")


def _urlopen_via_certifi(url, *, context=None, timeout=5.0):
    """clients.clock_safety.probe_exchange_clock's own ssl.create_default_context() relies on the OS
    trust store, which this host's python.org framework build cannot resolve (CERTIFICATE_VERIFY_FAILED
    - a local environment gap, not a clock-safety defect: BitgetRestClient's own requests-based calls,
    which bundle certifi's CA file, work fine against the same host all session). Reusing
    probe_exchange_clock's real logic and bounds with only the SSL context swapped for a working one,
    rather than re-implementing the probe."""
    import ssl as _ssl
    import certifi
    import urllib.request as _urllib_request
    ctx = _ssl.create_default_context(cafile=certifi.where())
    return _urllib_request.urlopen(url, context=ctx, timeout=timeout)


def gate_clock() -> GateResult:
    probe = probe_exchange_clock(urlopen=_urlopen_via_certifi)
    if probe.status != "OK":
        return GateResult("clock", False, f"status={probe.status} detail={probe.detail}")
    bound = max(abs(probe.offset_lower_ms), abs(probe.offset_upper_ms))
    if bound > CLOCK_OFFSET_LIMIT_MS:
        return GateResult("clock", False, f"offset bound {bound:.1f}ms exceeds {CLOCK_OFFSET_LIMIT_MS:.0f}ms limit")
    return GateResult("clock", True, f"offset bound {bound:.1f}ms, rtt={probe.rtt_ms:.0f}ms")


def gate_capacity() -> GateResult:
    free = _disk_free_gb()
    return GateResult("capacity", free >= DISK_FREE_FLOOR_GB, f"{free:.2f} GiB free (floor {DISK_FREE_FLOOR_GB} GiB)")


def gate_risk_manager() -> GateResult:
    try:
        rm = RiskManager(Settings())
        return GateResult("risk_manager", True, f"RiskManager constructed ok, class={type(rm).__name__}")
    except Exception as exc:
        return GateResult("risk_manager", False, f"{type(exc).__name__}: {exc}")


def gate_kill_switch() -> GateResult:
    try:
        rm = RiskManager(Settings())
        candidate = SimpleNamespace(strategy="signal_core_funnel_v1", symbol="BTCUSDT", direction="LONG", notes=[])
        rm._kill_switch_gate(candidate)
        return GateResult("kill_switch", True, "kill-switch gate callable and returned a verdict")
    except Exception as exc:
        return GateResult("kill_switch", False, f"{type(exc).__name__}: {exc}")


def gate_exposure_audit() -> GateResult:
    details = []
    for variant in VARIANTS:
        status_path = _paths(variant)["status"]
        if not status_path.exists():
            return GateResult("exposure_audit", False, f"{variant}: no status file yet (not started?)")
        try:
            doc = json.loads(status_path.read_text())
        except (OSError, ValueError) as exc:
            return GateResult("exposure_audit", False, f"{variant}: unreadable status file: {exc}")
        details.append(f"{variant}: nav={doc.get('nav')} trades={doc.get('n_trades_closed')} "
                       f"feed={doc.get('feed_health')}")
    return GateResult("exposure_audit", True, "; ".join(details))


def gate_continuous_paper_running() -> GateResult:
    problems = []
    for variant in VARIANTS:
        pid_path = REPO_ROOT / "state" / f"continuous_paper_{variant}.pid"
        status_path = _paths(variant)["status"]
        if not pid_path.exists():
            problems.append(f"{variant}: no pidfile")
            continue
        pid = int(pid_path.read_text().strip())
        try:
            os.kill(pid, 0)
        except OSError:
            problems.append(f"{variant}: pid {pid} not alive")
            continue
        if status_path.exists():
            age_s = time.time() - status_path.stat().st_mtime
            if age_s > 180:
                problems.append(f"{variant}: status file stale ({age_s:.0f}s old)")
    return GateResult("continuous_paper_running", not problems, "all running" if not problems else "; ".join(problems))


def gate_restart_reconciliation() -> GateResult:
    ok_details = []
    for variant in VARIANTS:
        state_path = _paths(variant)["state"]
        if not state_path.exists():
            ok_details.append(f"{variant}: no snapshot yet (fresh start, nothing to reconcile)")
            continue
        try:
            json.loads(state_path.read_text())
        except (OSError, ValueError) as exc:
            return GateResult("restart_reconciliation", False, f"{variant}: state.json unreadable: {exc}")
        ok_details.append(f"{variant}: state.json readable")
    return GateResult("restart_reconciliation", True, "; ".join(ok_details))


def gate_protection_semantics() -> GateResult:
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_funnel_restoration_dry_run.py::test_a_synthetic_candidate_proves_scorer_through_flat_close",
         "tests/test_funnel_restoration_dry_run.py::test_a_synthetic_candidate_that_hits_its_stop_also_ends_flat_with_nothing_resting",
         "-q"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    passed = result.returncode == 0
    return GateResult("protection_semantics", passed,
                      "TP-flat and SL-flat synthetic lifecycle tests pass" if passed
                      else f"FAILED:\n{result.stdout[-1500:]}\n{result.stderr[-500:]}")


ALL_GATES = (gate_baseline_unchanged, gate_repo_clean, gate_clock, gate_capacity, gate_risk_manager,
            gate_kill_switch, gate_exposure_audit, gate_continuous_paper_running,
            gate_restart_reconciliation, gate_protection_semantics)


def _read_tty_line(prompt: str, timeout_s: float, max_len: int) -> bytes:
    fd = sys.stdin.fileno()
    if not os.isatty(fd) or not os.isatty(sys.stdout.fileno()):
        raise Refused("INTERACTIVE_ONLY: this ceremony must be run at a real terminal, not piped/redirected")
    termios.tcflush(fd, termios.TCIFLUSH)
    print(prompt, end="", flush=True)
    deadline = time.monotonic() + timeout_s
    data = b""
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise Refused("TIMEOUT: no input received in time")
        chunk = os.read(fd, 128)
        if not chunk:
            raise Refused("EOF")
        data += chunk
        if len(data) > max_len + 1:
            raise Refused("PHRASE_MISMATCH")
    return data


class Ticket:
    """In-memory, single-use, expiring. No file, no serialization, no cross-process replay."""

    def __init__(self, ttl_s: float = TICKET_TTL_S) -> None:
        self._issued_at = time.monotonic()
        self._ttl_s = ttl_s
        self._consumed = False

    def consume(self) -> None:
        if self._consumed:
            raise Refused("TICKET_ALREADY_CONSUMED")
        if time.monotonic() - self._issued_at > self._ttl_s:
            raise Refused("TICKET_EXPIRED")
        self._consumed = True


def run_gates() -> list[GateResult]:
    results = []
    for gate_fn in ALL_GATES:
        result = gate_fn()
        results.append(result)
        print(f"  [{'PASS' if result.passed else 'FAIL'}] {result.name}: {result.detail}", flush=True)
    return results


def print_status_summary() -> None:
    print()
    print("=" * 60)
    print("MODE=DEMO/PAPER")
    print("REAL_MONEY=NO")
    print("=" * 60)
    for variant in VARIANTS:
        status_path = _paths(variant)["status"]
        if not status_path.exists():
            print(f"{variant}: NOT STARTED")
            continue
        doc = json.loads(status_path.read_text())
        print(f"{variant.upper()} ({doc['label']}, cap={doc['cap_pct']}%): pid={doc['pid']} "
             f"uptime={doc['uptime_s']:.0f}s nav={doc['nav']} trades_closed={doc['n_trades_closed']} "
             f"signals={doc['n_signals']} accepted={doc['n_accepted']}")
    print()


def main() -> int:
    print("SIGNAL_CORE_FUNNEL_V1 - OWNER DEMO CEREMONY")
    print("This ceremony can NEVER place a real order. It only confirms the DEMO/PAPER bot is healthy")
    print("and lets you start watching it. No credentials are used anywhere on this path.")
    print()
    print("Step 1 of 3: running safety checks...")
    results = run_gates()
    failed = [r for r in results if not r.passed]
    if failed:
        print()
        print(f"REFUSED: {len(failed)} gate(s) failed: {[r.name for r in failed]}")
        print("Fix the failing gate(s) above and re-run this ceremony. Nothing was started or authorized.")
        return 2

    print()
    print("Step 2 of 3: all safety checks passed. Readiness summary:")
    print_status_summary()

    ticket = Ticket()
    try:
        data = _read_tty_line(f"Type exactly '{PHRASE}' to confirm you want to watch the DEMO/PAPER bot: ",
                              INPUT_TIMEOUT_S, len(PHRASE))
    except Refused as exc:
        print(f"\nREFUSED: {exc}")
        return 3
    if data != PHRASE.encode() + b"\n":
        print("\nREFUSED: PHRASE_MISMATCH")
        return 3
    try:
        ticket.consume()
    except Refused as exc:
        print(f"\nREFUSED: {exc}")
        return 3

    print()
    print(f"CONFIRMED. Phrase meaning: {PHRASE_MEANING}.")
    print("Step 3 of 3: proving the full mechanical chain with a separately-tagged synthetic test")
    print("(this does NOT touch the live continuous-PAPER bot's own state):")
    proof = gate_protection_semantics()
    print(f"  [{'PASS' if proof.passed else 'FAIL'}] lifecycle_proof: {proof.detail if not proof.passed else 'OK'}")
    print()
    print_status_summary()
    print("DEMO/PAPER is running. Nothing further is required from you. This ceremony never armed real")
    print("money and has no path to do so. To stop the bot, use the shutdown instructions in")
    print("START_DEMO.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
