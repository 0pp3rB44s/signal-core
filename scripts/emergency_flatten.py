#!/usr/bin/env python3
"""Owner-triggered emergency flatten through the lifecycle-aware orchestrator.

Nothing happens without the exact confirmation flag. This command is never
started by the runner, supervisor, dashboard, or deployment process.
"""

from __future__ import annotations

import argparse
import json

from app.config import get_settings
from execution.position_manager import PositionManager


CONFIRM_FLAG = "--confirm-emergency-flatten"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        CONFIRM_FLAG,
        action="store_true",
        help="confirm exchange position closes and lifecycle reconciliation",
    )
    args = parser.parse_args(argv)
    if not args.confirm_emergency_flatten:
        parser.error(f"{CONFIRM_FLAG} is required; no exchange action was taken")

    result = PositionManager(settings=get_settings()).emergency_flatten_all()
    summary = {
        "status": result.get("status"),
        "positions_found": result.get("positions_found"),
        "closed_count": len(result.get("closed") or []),
        "error_count": len(result.get("errors") or []),
        "recording_outcomes": result.get("recording_outcomes") or [],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if not result.get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
