#!/bin/bash
# Hourly observation snapshot for the 72h validation. Read-only: it never
# restarts, stops or steers the bot. One JSON file per snapshot.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
OUT="validation_72h/snapshots/$(date -u +%Y%m%dT%H%M%SZ).json"
BOTPID="$(cat state/bot.pid 2>/dev/null || echo '')"
HEALTH="$(bash scripts/check_forward_paper.sh 2>/dev/null | awk -F= '$1=="status"{print $2}' | tail -1)"

.venv/bin/python - "$OUT" "$BOTPID" "$HEALTH" <<'PY'
import json, os, subprocess, sys, time
from pathlib import Path
out, botpid, health = sys.argv[1], sys.argv[2], sys.argv[3]

def alive(pid):
    try: os.kill(int(pid), 0); return True
    except Exception: return False

def sh(cmd):
    try: return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return ""

snap = {
    "snapshot_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "health_status": health or "UNKNOWN",
    "bot_pid": botpid, "bot_alive": alive(botpid) if botpid else False,
    "supervisor_alive": bool(sh("pgrep -f forward_paper_keepalive | head -1")),
    "power_assertion": bool(sh(f"pgrep -f 'caffeinate.*{botpid}' | head -1")) if botpid else False,
    "archiver_alive": bool(sh("pgrep -f archiving.run_archiver | head -1")),
}
# heartbeat
try:
    hb = json.loads(Path("state/runtime_heartbeat.json").read_text())
    snap["heartbeat"] = {k: hb.get(k) for k in ("stage","updated_at","scan_cycles_started","scan_cycles_completed")}
    snap["heartbeat_details"] = hb.get("details")
except Exception as e:
    snap["heartbeat_error"] = str(e)
# paper store
try:
    from forward_paper.store import ForwardPaperEventStore
    st = ForwardPaperEventStore("data_store/forward_paper_events.jsonl")
    evs = st.read_events()
    from collections import Counter
    snap["paper"] = {
        "chain": "VALID", "events": len(evs),
        "event_types": dict(sorted(Counter(e["event_type"] for e in evs).items())),
        "opened": sum(e["event_type"] == "TRADE_OPENED" for e in evs),
        "closed": sum(e["event_type"] == "TRADE_CLOSED" for e in evs),
    }
except Exception as e:
    snap["paper"] = {"chain": "INVALID", "error": f"{type(e).__name__}: {e}"}
# funnel counters
try:
    n = executable = 0
    with open("data_store/funnel_events.jsonl") as f:
        for line in f:
            n += 1
            if '"EXECUTABLE_DECISION"' in line and '"PASS"' in line: executable += 1
    snap["funnel"] = {"total_events": n, "executable_pass_cumulative": executable}
except Exception as e:
    snap["funnel"] = {"error": str(e)}
# resources / errors
snap["disk_avail"] = sh("df -h . | tail -1 | awk '{print $4}'")
snap["logs_size"] = sh("du -sh logs 2>/dev/null | cut -f1")
snap["proc"] = sh(f"ps -p {botpid} -o %cpu,%mem,etime | tail -1") if botpid else ""
snap["errors"] = {
    "failed_closed": int(sh("grep -c FORWARD_PAPER_FAILED_CLOSED logs/forward_paper.out 2>/dev/null") or 0),
    "scan_cycle_failed": int(sh("grep -c SCAN_CYCLE_FAILED logs/forward_paper.out 2>/dev/null") or 0),
    "scan_recovered": int(sh("grep -c SCAN_CYCLE_RECOVERED logs/forward_paper.out 2>/dev/null") or 0),
    "keepalive_restarts": int(sh("grep -c 'herstart gelukt' logs/forward_paper_keepalive.log 2>/dev/null") or 0),
}
Path(out).write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
print(f"{snap['snapshot_utc']} health={snap['health_status']} events={snap.get('paper',{}).get('events')} "
      f"exec={snap.get('funnel',{}).get('executable_pass_cumulative')} bot_alive={snap['bot_alive']}")
PY
