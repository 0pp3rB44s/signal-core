"""THE ONLY WAY LSRL MICRO_LIVE CAN TRADE: build the runtime WITHOUT trading authority, prove every gate on it, let the owner type the phrase, then promote.

    .venv/bin/python -m lsrl.tools.live_launcher --policy <release_policy.json> --monitor-dir <capacity monitor dir>
    .venv/bin/python -m lsrl.tools.live_launcher --protect-only --monitor-dir <dir>       # the ONLY mode a restart / launchd respawn may use
    .venv/bin/python -m lsrl.tools.live_launcher --status                                   # which gates are met; changes nothing

THE ORDER (each step refuses; nothing later runs after a refusal):
  1. The OWNER types the SHA-256 of the release policy at a real terminal (never a flag, variable, file or pipe): the policy cannot vouch for itself.
  2. The policy is loaded (typed digest, decision, expiry <= 7 d, capital inside the 100-150 USDT band, four pinned artifacts).
  3. A MICRO_LIVE runtime is built with the PROTECT_ONLY authority and started: real feed, real recorder, real risk manager, real executor reconciling the exchange,
     unable to open a position. All health gates are observed on THIS runtime, never on PAPER.
  4. `owner_ceremony.ceremony(provider)` validates all 19 gates (alpha evidence, engineering approval, process-down attestation, exchange semantics, account-wide
     flat audit, clock, feed, recorder, disk, RiskManager, kill switches, reconciliation, ...) and only then prints the readiness banner and asks for the exact
     phrase `START LIVE PILOT` on the controlling terminal. A refusal stops here: the phrase is never even requested.
  5. Only after the phrase does `Permit.consume` re-validate and return the identity; `LiveAuthority.trade` accepts nothing else; `runtime.promote` swaps it in.

RESTARTS. The permit is in memory and single-use. A crash, a reboot or a launchd respawn starts at step 3 with `--protect-only` (there is no other way to start
it): the executor reconciles, keeps every owned position stopped or flattens it, cancels any resting entry of the previous life and opens nothing. Trading again
needs the whole ceremony again, by the owner, at a terminal.

WHAT THE OWNER MUST HAVE ON DISK (none exists today, so today every run ends NO_GO before the phrase): the release policy and the four artifacts it pins.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lsrl.tools import owner_ceremony as oc
from lsrl.tools.ceremony_provider import Collectors, ProductionProvider
from lsrl.tools.release_policy import PolicyRefused, ReleasePolicy, load_policy

WARMUP_S = 60.0
WARMUP_TIMEOUT_S = 240.0


@dataclass
class Stage:
    """The proposed live runtime, started WITHOUT trading authority."""
    runtime: Any
    settings: Any
    client: Any
    risk_manager: Any
    capacity_gate: Any
    volume: str
    started_mono: float
    stop: Callable[[], None]


@dataclass
class Deps:
    read_digest: Callable[[str], str] = oc.read_pinned_digest
    load_policy: Callable[..., ReleasePolicy] = load_policy
    build_stage: Callable[..., Stage] | None = None
    collectors: Callable[[Stage, ReleasePolicy], Collectors] | None = None
    ceremony: Callable[[Any], Any] = oc.ceremony
    run_forever: Callable[[Stage], int] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    is_terminal: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty()


def _out(doc: dict[str, Any]) -> None:
    print(json.dumps(doc, sort_keys=True), flush=True)


def no_go(reason: str) -> int:
    _out({"LIVE_DECISION": "NO_GO", "reason": reason, "PRODUCTION_ARMED": "NO", "REAL_ORDER_PLACED": "NO"})
    return 2


# ---------------------------------------------------------------------------------------------------- the real pieces (owner's environment)
def real_stage(args: argparse.Namespace, policy: ReleasePolicy | None) -> Stage:
    from app.config import Settings
    from clients.bitget_rest import BitgetRestClient
    from execution.execution_service import ExecutionService
    from lsrl.adapters.authority import LiveAuthority
    from lsrl.adapters.capacity_gate import MonitorCapacityGate
    from lsrl.runtime import build_runtime
    from risk.risk_manager import RiskManager

    settings = Settings()
    client = BitgetRestClient(settings=settings)
    risk_manager = RiskManager(settings=settings)
    runtime = build_runtime(settings, client=client, execution_service=ExecutionService(settings=settings), risk_manager=risk_manager,
                            project_dir=Path(__file__).resolve().parents[2], authority=LiveAuthority.protect_only())
    runtime.start()
    return Stage(runtime, settings, client, risk_manager, MonitorCapacityGate(args.monitor_dir), args.volume, time.monotonic(), runtime.stop)


def real_collectors(stage: Stage, policy: ReleasePolicy) -> Collectors:
    from lsrl.adapters.authority import runtime_config_digest
    from lsrl.core.config import DEFAULT_CONFIG
    from lsrl.tools import gate_collectors as g
    from lsrl.tools.exposure_audit import audit

    project = Path(__file__).resolve().parents[2]
    return Collectors(
        git=lambda: oc.git_facts(project),
        equity=lambda: float(stage.runtime.equity_fn()),
        runtime_config_hash=lambda p: runtime_config_digest(stage.settings, DEFAULT_CONFIG, capital_ceiling_usdt=p.capital.capital_ceiling_usdt,
                                                            exposure_ceiling_usdt=p.capital.exposure_ceiling_usdt),
        accept_demo=lambda: getattr(stage.settings, "lsrl_accept_demo_process_down_evidence", False) is True,
        audit=lambda: audit(stage.client),
        clock=lambda: g.clock_gate(stage.client),
        feed=lambda: g.feed_gate(stage.runtime, uptime_s=time.monotonic() - stage.started_mono),
        recorder=lambda: g.recorder_gate(stage.runtime),
        disk=lambda: g.disk_gate(stage.capacity_gate, stage.volume),
        risk_manager=lambda: g.risk_manager_gate(stage.runtime, stage.risk_manager),
        kill_switches=lambda: g.kill_switch_gate(stage.risk_manager, stage.settings),
        reconciliation=lambda: g.reconciliation_gate(stage.runtime),
    )


def real_run_forever(stage: Stage) -> int:
    running = {"go": True}

    def stop(_signum, _frame) -> None:
        running["go"] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running["go"]:
            time.sleep(1.0)
    finally:
        stage.stop()                    # leaves every exchange-side stop and protective order exactly where it is
    return 0


# ---------------------------------------------------------------------------------------------------- the launch
def launch(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    ap = argparse.ArgumentParser(description="The only launcher for LSRL MICRO_LIVE")
    ap.add_argument("--policy", type=Path, help="the release policy file (its SHA-256 is typed by the owner at the terminal)")
    ap.add_argument("--monitor-dir", type=Path, help="the capacity monitor's output directory (its heartbeat and alarms gate new entries)")
    ap.add_argument("--volume", default="/System/Volumes/Data" if sys.platform == "darwin" else "/")
    ap.add_argument("--protect-only", action="store_true", help="reconcile, protect and cancel only; never opens a position (the restart mode)")
    ap.add_argument("--status", action="store_true", help="print which artifacts exist; contacts nothing, changes nothing")
    args = ap.parse_args(argv)
    d = deps or Deps()
    d.build_stage = d.build_stage or real_stage
    d.collectors = d.collectors or real_collectors
    d.run_forever = d.run_forever or real_run_forever

    if args.status:
        _out({"policy_present": bool(args.policy and Path(args.policy).is_file()), "PRODUCTION_ARMED": "NO", "LIVE_DECISION": "NO_GO",
              "note": "no gate is evaluated by --status; run the launcher on a terminal to evaluate them"})
        return 2
    if args.monitor_dir is None:
        return no_go("MONITOR_DIR_REQUIRED")
    if args.protect_only:
        stage = d.build_stage(args, None)
        _out({"mode": "PROTECT_ONLY", "note": "reconciling and protecting only; no position can be opened by this process"})
        return d.run_forever(stage)

    if args.policy is None:
        return no_go("POLICY_REQUIRED")
    if not d.is_terminal():
        return no_go("INTERACTIVE_ONLY")                                   # before the digest is even requested
    try:
        digest = d.read_digest("release policy")
        policy = d.load_policy(args.policy, digest)
    except (oc.Refused, PolicyRefused) as exc:
        return no_go(str(exc))
    stage = d.build_stage(args, policy)
    try:
        deadline = d.monotonic() + WARMUP_TIMEOUT_S
        while d.monotonic() - stage.started_mono < WARMUP_S and d.monotonic() < deadline:
            d.sleep(1.0)                                                   # the health gates need a warmed-up runtime to observe
        provider = ProductionProvider(policy, d.collectors(stage, policy))
        permit = d.ceremony(provider)
        identity = permit.consume(provider)
    except oc.Refused as exc:
        stage.stop()
        return no_go(str(exc))
    except Exception as exc:                                               # noqa: BLE001 - an unexpected failure is a refusal
        stage.stop()
        return no_go(f"UNEXPECTED:{type(exc).__name__}")
    from lsrl.adapters.authority import LiveAuthority
    stage.runtime.promote(LiveAuthority.trade(identity, capacity_gate=stage.capacity_gate))
    _out({"mode": "TRADE", "PRODUCTION_ARMED": "YES_BY_OWNER_CEREMONY", "capital_ceiling_usdt": identity.capital_ceiling_usdt,
          "exposure_ceiling_usdt": identity.exposure_ceiling_usdt})
    return d.run_forever(stage)


def main() -> int:
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
