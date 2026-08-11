#!/usr/bin/env python3
"""Atomically pin an existing live environment to LOW_VOL_RECLAIM_V2 only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import tempfile


LIVE_VALUES = {
    "EXECUTOR_ID": "runner01",
    "HOST_ID": "runner-mba01",
    "STRATEGY_ISOLATION_ENABLED": "true",
    "ENABLED_STRATEGIES": "low_vol_reclaim_v2",
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED": "false",
    "DYNAMIC_GRID_ENABLED": "false",
    "DYNAMIC_GRID_MODE": "OFF",
    "MAKER_ENTRY_FALLBACK_MARKET": "false",
}


def configure(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"refusing non-regular environment file: {path}")
    original = path.read_text(encoding="utf-8")
    seen: set[str] = set()
    output: list[str] = []
    for line in original.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in LIVE_VALUES:
            if key not in seen:
                output.append(f"{key}={LIVE_VALUES[key]}")
                seen.add(key)
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    for key, value in LIVE_VALUES.items():
        if key not in seen:
            output.append(f"{key}={value}")
    output.append("")

    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env.live"))
    args = parser.parse_args()
    configure(args.env_file)
    print("LOW_VOL_RECLAIM_V2 live configuration pinned; credentials unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
