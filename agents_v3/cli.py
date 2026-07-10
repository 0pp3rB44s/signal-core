from __future__ import annotations

import argparse

from agents_v3.orchestrator.orchestrator import run


VALID_MODES = {"audit", "plan", "patch", "test", "status", "safety", "propose", "do", "improve", "auto", "analyze", "agent", "cycle"}


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cgcagent",
        description="CGCAgent - local Codex-style engineering agent for the tradingbot.",
    )
    parser.add_argument("mode", choices=sorted(VALID_MODES))
    parser.add_argument("task", nargs="+", help="Task for CGCAgent")
    parser.add_argument("--approve", action="store_true", help="Allow CGCAgent to apply approved actions")

    args = parser.parse_args()
    return run(args.mode, " ".join(args.task), approved=args.approve)


if __name__ == "__main__":
    raise SystemExit(main())
