"""Fault-tolerant data access with provenance attached to every read.

Nothing in this dashboard reads a file directly. Every read goes through here so
that a missing file, a truncated write or a corrupt payload becomes a *described*
condition rather than a stack trace, and so that every number on screen can name
its origin and age.

Design rules:
  * never raise into a panel - a bad widget must not take the page down;
  * never silently substitute a default for missing data - say UNKNOWN;
  * never present a file's contents without also exposing its age.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections import deque

from dashboard_v3.core.status import Status, freshness

BASE_PATH = Path(__file__).resolve().parents[2]

#: Per-source staleness policy, in seconds. Chosen from the cadence each file is
#: actually written at, not from a single global guess.
STALE_POLICY: dict[str, tuple[float, float | None]] = {
    # path                                   (stale_after, offline_after)
    "state/runtime_heartbeat.json": (600, 3600),
    "state/watchdog_heartbeat.json": (3600, None),
    "state/account_equity.json": (3600, None),
    "state/portfolio_equity_guard.json": (900, 7200),
    "data_store/trades/daily_learning_report.json": (3600, 86400),
    "state/executed_trades.json": (900, 7200),
    "data_store/funnel_events.jsonl": (900, 7200),
    "data_store/dynamic_grid_v1_events.jsonl": (900, 7200),
    "state/dynamic_grid_v1.json": (900, 7200),
    "state/dynamic_grid_v1_shadow.json": (900, 7200),
    "logs/live.out": (900, 7200),
    "logs/trade_plans.csv": (3600, None),
    "reports/backtests/strategy_expectancy.json": (86400 * 2, None),
    "reports/backtests/latest_summary.json": (86400 * 7, None),
    "logs/trade_dataset_v2.csv": (86400 * 7, None),
    "logs/alerts.log": (86400 * 30, None),
    "data_store/research_liq_oi/status.json": (120, 600),
    "data_store/research_binance/status.json": (120, 600),
    "data_store/research_binance_spot/status.json": (120, 600),
    "data_store/microflow_live/status.json": (120, 600),
    "data_store/microflow-v1/status.json": (120, 600),
}
DEFAULT_POLICY = (86400.0, None)


@dataclass
class Provenance:
    """Where a number came from, when, and whether it can be trusted."""

    source: str
    kind: str = "file"          # file | api | derived | config | process
    exists: bool = True
    parsed: bool = True
    error: str = ""
    mtime_epoch: float | None = None
    size_bytes: int | None = None
    rows: int | None = None
    note: str = ""

    @property
    def age_seconds(self) -> float | None:
        if self.mtime_epoch is None:
            return None
        return max(0.0, time.time() - self.mtime_epoch)

    @property
    def status(self) -> Status:
        if not self.exists:
            return Status.UNKNOWN
        if not self.parsed:
            return Status.DEGRADED
        stale_after, offline_after = STALE_POLICY.get(self.source, DEFAULT_POLICY)
        return freshness(self.age_seconds, stale_after=stale_after, offline_after=offline_after)

    @property
    def age_label(self) -> str:
        age = self.age_seconds
        if age is None:
            return "unknown"
        if age < 90:
            return f"{int(age)}s"
        if age < 5400:
            return f"{age / 60:.0f}m"
        if age < 172800:
            return f"{age / 3600:.1f}h"
        return f"{age / 86400:.1f}d"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "exists": self.exists,
            "parsed": self.parsed,
            "error": self.error,
            "age_seconds": self.age_seconds,
            "age_label": self.age_label,
            "status": self.status.value,
            "rows": self.rows,
            "size_bytes": self.size_bytes,
            "note": self.note,
        }


@dataclass
class Loaded:
    """A payload plus its provenance. ``ok`` means the payload is trustworthy."""

    value: Any
    provenance: Provenance
    default_used: bool = False

    @property
    def ok(self) -> bool:
        return self.provenance.exists and self.provenance.parsed

    @property
    def status(self) -> Status:
        return self.provenance.status


def _stat(path: Path) -> tuple[float | None, int | None]:
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None, None


def load_json(rel: str, default: Any = None) -> Loaded:
    """Read a JSON file. Missing -> UNKNOWN, corrupt -> DEGRADED, never raises."""
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded(default, Provenance(rel, exists=False, parsed=False,
                                          error="file not found"), True)
    mtime, size = _stat(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Loaded(default, Provenance(rel, parsed=False, error=f"unreadable: {exc}",
                                          mtime_epoch=mtime, size_bytes=size), True)
    if not text.strip():
        return Loaded(default, Provenance(rel, parsed=False, error="empty file",
                                          mtime_epoch=mtime, size_bytes=size), True)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        # A partial write looks exactly like this. Report it; do not guess.
        return Loaded(default, Provenance(rel, parsed=False,
                                          error=f"corrupt JSON: {exc.__class__.__name__}",
                                          mtime_epoch=mtime, size_bytes=size), True)
    rows = len(value) if isinstance(value, (list, dict)) else None
    return Loaded(value, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=rows))


def load_csv(rel: str, limit: int | None = 2000, tail: bool = True) -> Loaded:
    """Read a CSV into dicts. Large files are tailed, never fully buffered."""
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded([], Provenance(rel, exists=False, parsed=False,
                                     error="file not found"), True)
    mtime, size = _stat(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        return Loaded([], Provenance(rel, parsed=False, error=f"csv error: {exc}",
                                     mtime_epoch=mtime, size_bytes=size), True)
    total = len(rows)
    if limit is not None and total > limit:
        rows = rows[-limit:] if tail else rows[:limit]
    return Loaded(rows, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=total))


def load_jsonl_tail(rel: str, max_bytes: int = 4_000_000, limit: int = 4000) -> Loaded:
    """Tail a JSONL file. funnel_events.jsonl is ~78 MB; never read it whole."""
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded([], Provenance(rel, exists=False, parsed=False,
                                     error="file not found"), True)
    mtime, size = _stat(path)
    try:
        with path.open("rb") as fh:
            if size and size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                fh.readline()  # discard the partial first line
            blob = fh.read()
    except OSError as exc:
        return Loaded([], Provenance(rel, parsed=False, error=f"unreadable: {exc}",
                                     mtime_epoch=mtime, size_bytes=size), True)

    events: list[dict[str, Any]] = []
    bad = 0
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)
    if limit and len(events) > limit:
        events = events[-limit:]
    prov = Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=len(events))
    if bad:
        prov.note = f"{bad} unparseable line(s) skipped"
    return Loaded(events, prov)


def load_segment_tail(rel: str, segment_limit: int = 4, row_limit: int = 6000) -> Loaded:
    """Read only the newest immutable gzip segments, streaming into a bounded deque."""
    root = BASE_PATH / rel
    segment_dir = root / "segments"
    if not segment_dir.is_dir():
        return Loaded([], Provenance(rel, exists=False, parsed=False,
                                     error="segment directory not found"), True)
    try:
        segments = sorted(segment_dir.glob("*.jsonl.gz"), key=lambda p: p.stat().st_mtime)
        selected = segments[-max(1, segment_limit):]
        rows: deque[dict[str, Any]] = deque(maxlen=max(1, row_limit))
        bad = 0
        total_bytes = 0
        for path in selected:
            total_bytes += path.stat().st_size
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        mtime = max((path.stat().st_mtime for path in selected), default=None)
        prov = Provenance(rel, mtime_epoch=mtime, size_bytes=total_bytes, rows=len(rows),
                          note=f"{len(selected)}/{len(segments)} newest segment(s)")
        if bad:
            prov.note += f"; {bad} unparseable row(s) skipped"
        return Loaded(list(rows), prov)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return Loaded([], Provenance(rel, parsed=False,
                                     error=f"segment read failed: {type(exc).__name__}"), True)


def load_text_tail(rel: str, max_bytes: int = 400_000, lines: int = 400) -> Loaded:
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded([], Provenance(rel, exists=False, parsed=False,
                                     error="file not found"), True)
    mtime, size = _stat(path)
    try:
        with path.open("rb") as fh:
            if size and size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                fh.readline()
            blob = fh.read()
    except OSError as exc:
        return Loaded([], Provenance(rel, parsed=False, error=f"unreadable: {exc}",
                                     mtime_epoch=mtime, size_bytes=size), True)
    out = blob.decode("utf-8", errors="replace").splitlines()[-lines:]
    return Loaded(out, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=len(out)))


def file_provenance(rel: str) -> Provenance:
    """Provenance for a file we only care about the freshness of."""
    path = BASE_PATH / rel
    if not path.exists():
        return Provenance(rel, exists=False, parsed=False, error="file not found")
    mtime, size = _stat(path)
    return Provenance(rel, mtime_epoch=mtime, size_bytes=size)


def read_kv_state(rel: str) -> Loaded:
    """Parse a ``key=value`` state file such as state/live_runtime.state."""
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded({}, Provenance(rel, exists=False, parsed=False,
                                     error="file not found"), True)
    mtime, size = _stat(path)
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except OSError as exc:
        return Loaded({}, Provenance(rel, parsed=False, error=str(exc),
                                     mtime_epoch=mtime, size_bytes=size), True)
    return Loaded(data, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=len(data)))


def repo_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(BASE_PATH),
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def git_ref(ref: str) -> str:
    """Resolve a local git ref without fetching or changing repository state."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", ref], cwd=str(BASE_PATH),
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def worktree_status(limit: int = 40) -> dict[str, Any]:
    """Read-only porcelain snapshot; bounded so a dirty tree cannot flood the UI."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=str(BASE_PATH), capture_output=True, text=True, timeout=8, check=True,
        )
        rows = out.stdout.splitlines()
        return {"known": True, "clean": not rows, "rows": rows[:limit], "total": len(rows)}
    except Exception as exc:
        return {"known": False, "clean": None, "rows": [], "total": None,
                "error": type(exc).__name__}


def read_first_line(rel: str) -> Loaded:
    """Read one small marker file. Missing and unreadable remain explicit."""
    path = BASE_PATH / rel
    if not path.exists():
        return Loaded(None, Provenance(rel, exists=False, parsed=False,
                                       error="file not found"), True)
    mtime, size = _stat(path)
    try:
        value = path.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        return Loaded(None, Provenance(rel, parsed=False, error=f"unreadable: {type(exc).__name__}",
                                       mtime_epoch=mtime, size_bytes=size), True)
    return Loaded(value or None, Provenance(rel, mtime_epoch=mtime, size_bytes=size))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def parse_etime(text: str) -> int | None:
    """Parse BSD/macOS ``ps -o etime`` — ``[[DD-]HH:]MM:SS`` — into seconds.

    macOS ps has no ``etimes`` (that is a Linux GNU extension); asking for it
    makes the entire -o spec fail, which is how engine uptime silently became
    UNKNOWN.
    """
    text = (text or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, (m, s) = 0, nums
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def process_info(pid: int | None) -> dict[str, Any]:
    """ps snapshot for one pid. Returns {} when the pid is gone."""
    if not pid or not pid_alive(pid):
        return {}
    try:
        out = subprocess.run(
            ["ps", "-o", "etime=,rss=,pcpu=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        parts = out.stdout.strip().split(None, 3)
        if len(parts) < 4:
            return {}
        return {
            "uptime_seconds": parse_etime(parts[0]),
            "rss_kb": int(parts[1]),
            "cpu_pct": float(parts[2]),
            "started": parts[3].strip(),
        }
    except Exception:
        return {}


def matching_pids(pattern: str) -> list[int]:
    """All pids matching a pgrep pattern - used for duplicate-process detection."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                             text=True, timeout=5)
        return [int(p) for p in out.stdout.split() if p.isdigit()]
    except Exception:
        return []


def host_boot_epoch() -> float | None:
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5)
        for token in out.stdout.replace(",", " ").split():
            if token.isdigit() and len(token) >= 10:
                return float(token)
    except Exception:
        pass
    return None


__all__ = [
    "BASE_PATH", "Loaded", "Provenance", "file_provenance", "host_boot_epoch",
    "git_ref", "load_csv", "load_json", "load_jsonl_tail", "load_segment_tail", "load_text_tail",
    "matching_pids", "pid_alive", "process_info", "read_first_line", "read_kv_state",
    "repo_head", "worktree_status",
]
