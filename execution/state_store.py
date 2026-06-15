from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self.state_version = 1

    def load(self, default: Any) -> Any:
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

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._write_lock:
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
