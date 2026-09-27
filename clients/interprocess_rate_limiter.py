from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from telemetry.safe_io import file_lock


class InterprocessRateLimiter:
    """One timestamp budget shared by every local Bitget client process."""

    def __init__(self, state_path: str | Path, min_interval_seconds: float) -> None:
        self.state_path = Path(state_path).resolve()
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.log = logging.getLogger(self.__class__.__name__)

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return

        with file_lock(self.state_path):
            now = time.time()
            last_request, state_valid = self._read_timestamp(now)
            if not state_valid:
                # Unknown state means another request may just have happened.
                # Waiting a full interval is the only fail-closed assumption.
                sleep_seconds = self.min_interval_seconds
                self.log.warning(
                    "BITGET_RATE_LIMIT_STATE_INVALID | action=fail_closed_wait | sleep=%ss",
                    round(sleep_seconds, 4),
                )
            else:
                sleep_seconds = self.min_interval_seconds - (now - last_request)

            if sleep_seconds > 0:
                self.log.info(
                    "BITGET_RATE_LIMIT_WAIT | sleep=%ss | min_interval=%ss",
                    round(sleep_seconds, 4),
                    self.min_interval_seconds,
                )
                time.sleep(sleep_seconds)

            self._write_timestamp(time.time())

    def _read_timestamp(self, now: float) -> tuple[float, bool]:
        if not self.state_path.exists():
            return 0.0, True
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            timestamp = float(payload["last_request_epoch"])
            if timestamp < 0 or timestamp > now + self.min_interval_seconds:
                raise ValueError("timestamp outside valid range")
            return timestamp, True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return 0.0, False

    def _write_timestamp(self, timestamp: float) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"last_request_epoch": timestamp}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)
