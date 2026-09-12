"""Fault-tolerant, read-only access to the Sunday shadow runner's own state.

Deliberately self-contained rather than parameterising ``dashboard_v3.core.sources``:
the production data layer is working, tested code that every other panel depends on,
and this shadow observability feature is new and temporary. Duplicating a small,
bounded set of read primitives here means a bug in shadow parsing cannot regress any
existing panel, and removing shadow support later is a matter of deleting this file.

Design rules (matching core/sources.py):
  * never raise into a panel - a bad widget must not take the page down;
  * never silently substitute a default for missing data - say UNKNOWN;
  * PID_EXISTS is never treated as HEALTHY on its own.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Marks a directory as a real shadow checkout rather than an empty/wrong path.
_SHADOW_MARKERS = ("state", "data_store")


class ShadowRootError(RuntimeError):
    """Raised when CGC_SHADOW_ROOT does not point at a shadow checkout."""


def resolve_shadow_root() -> tuple[Path | None, str]:
    """Return (root, error). Never raises - shadow observability is optional.

    Unlike CGC_PROD_ROOT (which fails the whole dashboard closed), a missing or
    invalid shadow root degrades this one feature to UNKNOWN rather than taking
    down every other page - the shadow run is a temporary, secondary concern.
    """
    override = os.environ.get("CGC_SHADOW_ROOT", "").strip()
    if not override:
        return None, "CGC_SHADOW_ROOT not set"
    root = Path(override).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None, f"CGC_SHADOW_ROOT={override!r} does not exist"
    if not root.is_dir():
        return None, f"CGC_SHADOW_ROOT={override!r} is not a directory"
    missing = [m for m in _SHADOW_MARKERS if not (root / m).is_dir()]
    if missing:
        return None, f"CGC_SHADOW_ROOT={root} is missing: {', '.join(missing)}"
    return root, ""


SHADOW_ROOT, SHADOW_ROOT_ERROR = resolve_shadow_root()


@dataclass
class Provenance:
    source: str
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


@dataclass
class Loaded:
    value: Any
    provenance: Provenance

    @property
    def ok(self) -> bool:
        return self.provenance.exists and self.provenance.parsed


def _stat(path: Path) -> tuple[float | None, int | None]:
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None, None


def _missing(rel: str, default: Any) -> Loaded:
    return Loaded(default, Provenance(rel, exists=False, parsed=False,
                                       error="shadow root unavailable" if SHADOW_ROOT is None
                                       else "file not found"))


def load_json(rel: str, default: Any = None) -> Loaded:
    if SHADOW_ROOT is None:
        return _missing(rel, default)
    path = SHADOW_ROOT / rel
    if not path.exists():
        return _missing(rel, default)
    mtime, size = _stat(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Loaded(default, Provenance(rel, parsed=False, error=f"unreadable: {exc}",
                                           mtime_epoch=mtime, size_bytes=size))
    if not text.strip():
        return Loaded(default, Provenance(rel, parsed=False, error="empty file",
                                           mtime_epoch=mtime, size_bytes=size))
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return Loaded(default, Provenance(rel, parsed=False,
                                           error=f"corrupt JSON: {exc.__class__.__name__}",
                                           mtime_epoch=mtime, size_bytes=size))
    rows = len(value) if isinstance(value, (list, dict)) else None
    return Loaded(value, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=rows))


def load_csv(rel: str, limit: int | None = 2000) -> Loaded:
    if SHADOW_ROOT is None:
        return _missing(rel, [])
    path = SHADOW_ROOT / rel
    if not path.exists():
        return _missing(rel, [])
    mtime, size = _stat(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error) as exc:
        return Loaded([], Provenance(rel, parsed=False, error=f"csv error: {exc}",
                                      mtime_epoch=mtime, size_bytes=size))
    total = len(rows)
    if limit is not None and total > limit:
        rows = rows[-limit:]
    return Loaded(rows, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=total))


def load_jsonl(rel: str, limit: int | None = 5000) -> Loaded:
    """Read a JSONL file whole (shadow files are small - dozens to low thousands
    of lines during a single Sunday session, unlike production's segmented streams)."""
    if SHADOW_ROOT is None:
        return _missing(rel, [])
    path = SHADOW_ROOT / rel
    if not path.exists():
        return _missing(rel, [])
    mtime, size = _stat(path)
    rows: list[dict[str, Any]] = []
    bad = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError as exc:
        return Loaded([], Provenance(rel, parsed=False, error=f"unreadable: {exc}",
                                      mtime_epoch=mtime, size_bytes=size))
    if limit is not None and len(rows) > limit:
        rows = rows[-limit:]
    prov = Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=len(rows))
    if bad:
        prov.note = f"{bad} malformed line(s) skipped"
    return Loaded(rows, prov)


def read_kv_state(rel: str) -> Loaded:
    if SHADOW_ROOT is None:
        return _missing(rel, {})
    path = SHADOW_ROOT / rel
    if not path.exists():
        return _missing(rel, {})
    mtime, size = _stat(path)
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except OSError as exc:
        return Loaded({}, Provenance(rel, parsed=False, error=str(exc),
                                      mtime_epoch=mtime, size_bytes=size))
    return Loaded(data, Provenance(rel, mtime_epoch=mtime, size_bytes=size, rows=len(data)))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def process_cwd(pid: int | None) -> str | None:
    """The process's actual working directory - the authoritative way to tell a
    shadow engine apart from a production one that happens to share a module
    name, rather than trusting the command string alone."""
    if not pid or not pid_alive(pid):
        return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"],
                              capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        return None
    return None


def is_ancestor(older: str, newer: str) -> str:
    """Is ``older`` an ancestor of (or equal to) ``newer`` in the shadow repo -
    i.e. did ``older`` happen at or before ``newer`` in commit history?

    Returns 'YES' / 'NO' / 'UNKNOWN'. Argument order matches the plain-English
    question deliberately, after an earlier version of this helper's caller
    read the reversed git exit code and classified pre-hardening trades as
    post-hardening - the exact failure mode this file exists to prevent.
    """
    if SHADOW_ROOT is None or not older or not newer:
        return "UNKNOWN"
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", older, newer],
            cwd=str(SHADOW_ROOT), capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            return "YES"
        if result.returncode == 1:
            return "NO"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def git_ref(ref: str, root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--verify", ref], cwd=str(root),
                              capture_output=True, text=True, timeout=5, check=True)
        return out.stdout.strip()
    except Exception:
        return ""


__all__ = [
    "SHADOW_ROOT", "SHADOW_ROOT_ERROR", "Loaded", "Provenance",
    "is_ancestor", "git_ref", "load_csv", "load_json", "load_jsonl",
    "pid_alive", "process_cwd", "read_kv_state", "resolve_shadow_root",
]
