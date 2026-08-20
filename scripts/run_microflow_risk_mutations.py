#!/usr/bin/env python3
"""Bounded mutation check for the MicroFlow RiskManager integration.

Each mutant is applied only to a temporary repository copy.  A mutation is
counted as caught only when its designated green-path assertion fails; syntax
or collection errors are rejected as invalid mutation evidence.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/test_microflow_risk_integration.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    test: str


MUTATIONS = (
    Mutation(
        "remove_risk_manager_evaluate",
        "microflow/live.py",
        "risk = self.risk_manager.evaluate(\n            risk_candidate,",
        "risk = type(\"ForcedRisk\", (), {\"allowed\": True, \"account_risk_pct\": 0.75, \"reasons\": []})() if True else self.risk_manager.evaluate(\n            risk_candidate,",
        "test_microflow_build_fails_closed_when_risk_manager_blocks",
    ),
    Mutation(
        "force_evaluate_true",
        "risk/risk_manager.py",
        "        status = \"EXECUTABLE\" if allowed else \"BLOCKED\"\n",
        "        allowed = True  # MUTANT: force approval\n        status = \"EXECUTABLE\" if allowed else \"BLOCKED\"\n",
        "test_full_microflow_risk_path_blocks_loss_breakers",
    ),
    Mutation(
        "ignore_daily_loss",
        "risk/risk_manager.py",
        "        if hard_daily_stop_pct and daily_loss_pct >= hard_daily_stop_pct:\n",
        "        if False and hard_daily_stop_pct and daily_loss_pct >= hard_daily_stop_pct:\n",
        "test_full_microflow_risk_path_blocks_loss_breakers",
    ),
    Mutation(
        "ignore_correlated_exposure",
        "risk/risk_manager.py",
        "        if not cluster_allowed:\n            allowed = False\n",
        "        if False and not cluster_allowed:\n            allowed = False\n",
        "test_full_microflow_risk_path_blocks_correlated_exposure",
    ),
    Mutation(
        "ignore_consecutive_losses",
        "risk/risk_manager.py",
        "        if consecutive_losses >= 3:\n",
        "        if False and consecutive_losses >= 3:\n",
        "test_full_microflow_risk_path_blocks_loss_breakers",
    ),
    Mutation(
        "fail_open_on_exception",
        "microflow/live.py",
        "STRATEGY_ID = \"microflow_scalper_v1\"\n",
        "STRATEGY_ID = \"microflow_scalper_v1\"\n\n\ndef _mutant_fail_open(call, *args, **kwargs):\n    try:\n        return call(*args, **kwargs)\n    except Exception:\n        return type(\"ForcedRisk\", (), {\"allowed\": True, \"account_risk_pct\": 0.75, \"reasons\": []})()\n",
        "test_microflow_build_fails_closed_when_risk_evaluation_raises",
    ),
    Mutation(
        "wrong_account_equity",
        "microflow/live.py",
        "            observed_equity=equity,\n            proposed_notional_usdt=sizing.notional_usdt,",
        "            observed_equity=1.0,  # MUTANT: wrong account\n            proposed_notional_usdt=sizing.notional_usdt,",
        "test_microflow_passes_authenticated_equity_and_proposed_notional_to_risk",
    ),
    Mutation(
        "reset_breaker_on_restart",
        "app/equity.py",
        "        if PORTFOLIO_EQUITY_GUARD_PATH.exists():\n",
        "        if False and PORTFOLIO_EQUITY_GUARD_PATH.exists():\n",
        "test_equity_breaker_survives_risk_manager_restart",
    ),
)


def _apply_extra_fail_open_mutation(root: Path, mutation: Mutation) -> None:
    if mutation.name != "fail_open_on_exception":
        return
    path = root / "microflow/live.py"
    body = path.read_text()
    old = "risk = self.risk_manager.evaluate(\n            risk_candidate,"
    new = "risk = _mutant_fail_open(self.risk_manager.evaluate,\n            risk_candidate,"
    if body.count(old) != 1:
        raise RuntimeError("fail-open call-site mutation target is not unique")
    path.write_text(body.replace(old, new, 1))


def main() -> int:
    caught: list[str] = []
    invalid: list[str] = []
    with tempfile.TemporaryDirectory(prefix="microflow-risk-mutations-") as temp:
        temp_root = Path(temp) / "repo"
        for mutation in MUTATIONS:
            if temp_root.exists():
                shutil.rmtree(temp_root)
            shutil.copytree(
                ROOT,
                temp_root,
                ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"),
            )
            target = temp_root / mutation.path
            body = target.read_text()
            if body.count(mutation.old) != 1:
                invalid.append(f"{mutation.name}: target count={body.count(mutation.old)}")
                continue
            target.write_text(body.replace(mutation.old, mutation.new, 1))
            _apply_extra_fail_open_mutation(temp_root, mutation)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", f"{TEST_FILE}::{mutation.test}"],
                cwd=temp_root,
                text=True,
                capture_output=True,
            )
            output = result.stdout + result.stderr
            invalid_test_run = "ERROR collecting" in output or "SyntaxError" in output
            if result.returncode != 0 and "failed" in output.lower() and not invalid_test_run:
                caught.append(mutation.name)
            else:
                invalid.append(
                    f"{mutation.name}: rc={result.returncode} output={output[-500:].strip()}"
                )

    print(f"MUTATIONS_CAUGHT={len(caught)}/{len(MUTATIONS)}")
    for name in caught:
        print(f"CAUGHT {name}")
    for detail in invalid:
        print(f"INVALID_OR_SURVIVED {detail}")
    return 0 if len(caught) == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
