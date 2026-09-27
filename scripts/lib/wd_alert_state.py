#!/usr/bin/env python3
"""Alert deduplication, escalation and recovery state for watchdog_live.sh.

The existing send_alert() has no memory: every run re-sends every active fault.
At a 60s cadence that is an alert storm, and a storm is indistinguishable from
silence because the operator mutes the channel.

Rules implemented here:
  * first occurrence of a key delivers immediately;
  * repeats inside the cooldown are counted, not sent;
  * a severity escalation (HIGH -> CRITICAL) delivers immediately and resets it;
  * exactly one RESOLVED is emitted when a key clears, and ONLY if its opening
    alert was actually delivered — an undelivered incident must not produce a
    phantom "all clear";
  * state is written atomically; a corrupt state file is quarantined and
    treated as empty rather than crashing the watchdog.

Secrets are never read, logged or echoed here. Delivery is delegated to
scripts/lib/alert.sh, whose output is deliberately not captured beyond its exit
status so a provider response body can never reach a log.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEVERITY_RANK = {"INFO": 0, "WARN": 1, "HIGH": 2, "CRITICAL": 3}
DELIVER_TIMEOUT = 20
DELIVER_ATTEMPTS = 2
BACKOFF_SECONDS = 3


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # Corrupt or partially-written: quarantine, start clean, never crash.
        try:
            path.replace(path.with_suffix(path.suffix + f".corrupt_{int(time.time())}"))
        except Exception:
            pass
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def deliver(severity: str, event: str, message: str) -> bool:
    """Hand off to alert.sh. Returns True only on a proven success exit."""
    script = (
        'set -uo pipefail; . scripts/lib/alert.sh; '
        'send_alert "$1" "$2" "$3" >/dev/null 2>&1'
    )
    for attempt in range(1, DELIVER_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                ["bash", "-c", script, "_", severity, event, message],
                timeout=DELIVER_TIMEOUT, capture_output=True,
            )
            if proc.returncode == 0:
                return True
            # rc 3 = no provider configured. Retrying cannot help.
            if proc.returncode == 3:
                return False
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return False
        if attempt < DELIVER_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS)
    return False


def cmd_raise(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = load_state(path)
    now = int(args.now)
    entry = state.get(args.key)

    if entry is None:
        entry = {
            "key": args.key, "severity": args.severity, "message": args.message,
            "first_seen": now, "last_seen": now, "count": 1,
            "last_delivered": None, "delivered": False, "resolved_sent": False,
        }
        should_send = True
        reason = "first occurrence"
    else:
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = now
        entry["message"] = args.message
        entry["resolved_sent"] = False
        old_rank = SEVERITY_RANK.get(entry.get("severity", "INFO"), 0)
        new_rank = SEVERITY_RANK.get(args.severity, 0)
        last = entry.get("last_delivered")
        if new_rank > old_rank:
            should_send, reason = True, "severity escalation"
        elif last is None:
            should_send, reason = True, "not yet delivered"
        elif (now - int(last)) >= int(args.cooldown):
            should_send, reason = True, "cooldown elapsed"
        else:
            should_send = False
            reason = f"cooldown {int(args.cooldown) - (now - int(last))}s remaining"
        entry["severity"] = args.severity

    if should_send and args.no_deliver != "1":
        ok = deliver(entry["severity"], args.key, entry["message"])
        if ok:
            entry["last_delivered"] = now
            entry["delivered"] = True
        print(f"      alert {args.key}: {'SENT' if ok else 'NOT DELIVERED'} ({reason})")
    elif should_send:
        print(f"      alert {args.key}: suppressed by --no-deliver ({reason})")
    else:
        print(f"      alert {args.key}: deduplicated ({reason}, count={entry['count']})")

    state[args.key] = entry
    save_state(path, state)
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    path = Path(args.state)
    state = load_state(path)
    now = int(args.now)
    active = {k for k in (args.active or "").split(",") if k}

    for key, entry in list(state.items()):
        if key in active or not isinstance(entry, dict):
            continue
        if entry.get("resolved_sent"):
            state.pop(key, None)
            continue
        # No RESOLVED for an incident nobody was ever told about.
        if not entry.get("delivered"):
            state.pop(key, None)
            continue
        duration = now - int(entry.get("first_seen", now))
        message = (
            f"cleared after {duration // 60}m {duration % 60}s, "
            f"{entry.get('count', 0)} occurrence(s)"
        )
        if args.no_deliver != "1":
            ok = deliver("INFO", f"{key}_RESOLVED", message)
            print(f"      resolved {key}: {'SENT' if ok else 'NOT DELIVERED'}")
        else:
            print(f"      resolved {key}: suppressed by --no-deliver")
        state.pop(key, None)

    save_state(path, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("raise")
    r.add_argument("--state", required=True)
    r.add_argument("--key", required=True)
    r.add_argument("--severity", required=True)
    r.add_argument("--message", required=True)
    r.add_argument("--now", required=True)
    r.add_argument("--cooldown", default="1800")
    r.add_argument("--no-deliver", default="0")
    r.set_defaults(func=cmd_raise)

    v = sub.add_parser("resolve")
    v.add_argument("--state", required=True)
    v.add_argument("--now", required=True)
    v.add_argument("--active", default="")
    v.add_argument("--no-deliver", default="0")
    v.set_defaults(func=cmd_resolve)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
