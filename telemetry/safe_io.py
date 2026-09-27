from __future__ import annotations

import csv
import fcntl
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Serialize readers and writers across threads and local processes."""
    target = Path(path)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(lock_path), lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def locked_open(path: str | Path, mode: str, **kwargs: Any) -> Iterator[TextIO]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target):
        with target.open(mode, **kwargs) as handle:
            yield handle


def append_csv_rows(
    path: str | Path,
    *,
    fieldnames: list[str],
    rows: list[dict[str, Any]],
) -> None:
    """Append complete CSV records with exactly one header under concurrency."""
    if not rows:
        return
    with locked_open(path, "a+", newline="", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        empty = handle.tell() == 0
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if empty:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_json(path: str | Path, payload: Any, **dump_kwargs: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target):
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, **dump_kwargs)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
