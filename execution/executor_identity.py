"""Fail-closed identity and process lock for the authorised LIVE executor."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


_EXECUTOR_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,11}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ExecutionOwnershipError(RuntimeError):
    """LIVE execution cannot prove exclusive ownership."""


def _production_sha() -> str:
    deployed = Path("state/deployed_commit.txt")
    if deployed.is_file():
        value = deployed.read_text(encoding="utf-8").strip().lower()
        if _SHA_PATTERN.fullmatch(value):
            return value
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip().lower()
    except Exception:
        value = ""
    return value if _SHA_PATTERN.fullmatch(value) else ""


@dataclass(frozen=True)
class ExecutionIdentity:
    executor_id: str
    host_id: str
    pid: int
    production_sha: str
    credential_fingerprint: str
    client_id_namespace: str

    @classmethod
    def from_settings(cls, settings) -> "ExecutionIdentity":
        executor_id = str(getattr(settings, "executor_id", "") or "").strip().lower()
        if not _EXECUTOR_PATTERN.fullmatch(executor_id):
            raise ExecutionOwnershipError(
                "EXECUTOR_ID must be 3-12 lowercase client-id-safe characters"
            )
        configured_host = str(getattr(settings, "host_id", "") or "").strip().lower()
        hostname = socket.gethostname().strip().lower()
        host_id = configured_host or f"host-{hashlib.sha256(hostname.encode()).hexdigest()[:10]}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", host_id):
            raise ExecutionOwnershipError("HOST_ID is not stable/client-safe")
        production_sha = _production_sha()
        if not production_sha:
            raise ExecutionOwnershipError("production SHA cannot be proven")
        api_key = settings.bitget_api_key.get_secret_value()
        if not api_key:
            raise ExecutionOwnershipError("trading credential is missing")
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        namespace = f"cgc-{executor_id}"
        if len(namespace) > 16:
            raise ExecutionOwnershipError("executor namespace exceeds clientOid contract")
        return cls(
            executor_id=executor_id,
            host_id=host_id,
            pid=os.getpid(),
            production_sha=production_sha,
            credential_fingerprint=fingerprint,
            client_id_namespace=namespace,
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@contextmanager
def single_live_executor_lock(
    identity: ExecutionIdentity,
    path: str = "state/live_executor.lock",
) -> Iterator[None]:
    """Hold an exclusive non-blocking lock for the complete LIVE process."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionOwnershipError("another LIVE executor holds the lock") from exc
        handle.seek(0)
        handle.truncate()
        json.dump(identity.as_dict(), handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def is_owned_client_oid(client_oid: object, identity: ExecutionIdentity) -> bool:
    return str(client_oid or "").startswith(f"{identity.client_id_namespace}-")


def is_legacy_client_oid(client_oid: object) -> bool:
    return str(client_oid or "").startswith("bgai-")


__all__ = [
    "ExecutionIdentity",
    "ExecutionOwnershipError",
    "is_legacy_client_oid",
    "is_owned_client_oid",
    "single_live_executor_lock",
]
