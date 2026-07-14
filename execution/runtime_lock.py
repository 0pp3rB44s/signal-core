from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_process_lock = threading.RLock()


@contextmanager
def trading_state_lock(path: str = "state/trading_state.lock") -> Iterator[None]:
    """Serialize execution writes and position reconciliation."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock, lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
