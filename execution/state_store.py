from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import fcntl


T = TypeVar("T")


class JsonStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.state_version = 1

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Serialize state access across store instances and processes."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)

    def _load_unlocked(self, default: T) -> T:
        if not self.path.exists():
            return default

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)

            if isinstance(payload, dict) and "_state_metadata" in payload:
                metadata = payload.get("_state_metadata") or {}
                expected_checksum = str(metadata.get("checksum") or "")
                raw_data = payload.get("data")

                calculated_checksum = hashlib.sha256(
                    json.dumps(raw_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()

                if expected_checksum and expected_checksum != calculated_checksum:
                    raise ValueError(
                        f"State checksum mismatch for {self.path}: expected={expected_checksum} actual={calculated_checksum}"
                    )

                return raw_data if raw_data is not None else default

            return payload

        except json.JSONDecodeError as exc:
            self._quarantine_corrupt_file("json_decode_error", exc)
            return default
        except ValueError as exc:
            self._quarantine_corrupt_file("checksum_error", exc)
            return default
        except OSError as exc:
            self._quarantine_corrupt_file("os_error", exc)
            return default

    def load(self, default: T) -> T:
        with self._file_lock():
            return self._load_unlocked(default)

    def _quarantine_corrupt_file(self, reason: str, exc: Exception) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        quarantine_path = self.path.with_suffix(f"{self.path.suffix}.corrupt_{timestamp}")
        try:
            os.replace(str(self.path), str(quarantine_path))
            print(
                f"STATE_STORE_CORRUPT_QUARANTINED | path={self.path} | quarantine={quarantine_path} | reason={reason} | error={exc}"
            )
        except Exception as quarantine_exc:
            print(
                f"STATE_STORE_CORRUPT_QUARANTINE_FAILED | path={self.path} | reason={reason} | error={exc} | quarantine_error={quarantine_exc}"
            )

    def snapshot(self, max_snapshots: int = 10) -> Path | None:
        if not self.path.exists():
            return None

        snapshot_dir = self.path.parent / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_path = snapshot_dir / f"{self.path.name}.{timestamp}.snapshot"

        try:
            with self.path.open("rb") as source, snapshot_path.open("wb") as target:
                target.write(source.read())
        except OSError as exc:
            print(f"STATE_STORE_SNAPSHOT_FAILED | path={self.path} | error={exc}")
            return None

        snapshots = sorted(
            snapshot_dir.glob(f"{self.path.name}.*.snapshot"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for old_snapshot in snapshots[max_snapshots:]:
            try:
                old_snapshot.unlink()
            except OSError:
                pass

        return snapshot_path

    def _save_unlocked(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot(max_snapshots=10)

        wrapped_payload = {
            "_state_metadata": {
                "version": self.state_version,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "checksum": hashlib.sha256(
                    json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
            },
            "data": data,
        }

        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name,
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(wrapped_payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(tmp_name, str(self.path))
        except Exception:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except Exception:
                pass
            raise

    def save(self, data: Any) -> None:
        with self._write_lock, self._file_lock():
            self._save_unlocked(data)

    def update(self, default: T, mutator: Callable[[T], T | None]) -> T:
        """Atomically load, mutate and save state under one interprocess lock."""
        with self._write_lock, self._file_lock():
            current = self._load_unlocked(default)
            updated = mutator(current)
            result = current if updated is None else updated
            if result != current:
                self._save_unlocked(result)
            return result
