"""Exactly ONE owner per (symbol, hold side) of live exposure.

Why this exists: `PositionManager` adopts ANY exchange position that has no local record as a
`recovered_exchange_position` and then manages, "heals" and may close it. A position opened by a
strategy that runs its own order lifecycle (LSRL_V1) would be adopted the moment the position monitor
looked at it, and two owners would fight over one stop / one set of take-profits.

The registry is a small persisted claim table:

    state/strategy_ownership.json  ->  {"claims": {"SOLUSDT:long": {"owner": "lsrl_v1", "ref": <setup id>, ...}}}

  * a strategy CLAIMS (symbol, hold side) BEFORE it sends its first order and RELEASES it only when it has
    no orders and no exposure there any more;
  * the position monitor / recovery skips a claimed symbol; the audited entry submitter refuses an entry
    that belongs to a different owner than the current claim;
  * a claim by a second, different owner is refused (`OwnershipConflict`) -- never silently merged;
  * writes are atomic (temp file + fsync + rename) and serialized (thread lock + advisory file lock), so a
    restart reads exactly what was last written and recovery of ownership is deterministic.

An UNREADABLE registry never grants ownership silently: the strategy that owns the frozen symbol halts,
and legacy code protects that symbol (see `owned_symbols(fail_closed_symbols=...)`).
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_OWNERSHIP_PATH = "state/strategy_ownership.json"
#: Symbols legacy code must leave alone if the registry cannot be read (the frozen LSRL_V1 symbol).
PROTECTED_WHEN_UNREADABLE = ("SOLUSDT",)


class OwnershipConflict(RuntimeError):
    """Another owner already holds this (symbol, hold side)."""


class OwnershipUnreadable(RuntimeError):
    """The registry file exists but cannot be trusted."""


def _key(symbol: str, hold_side: str) -> str:
    return f"{str(symbol).upper()}:{str(hold_side).lower()}"


def owner_family(strategy: str) -> str:
    """`lsrl_v1_e3` -> `lsrl_v1`. Entry identities carry a leg suffix; ownership is per strategy."""
    text = str(strategy or "").strip().lower()
    for family in ("lsrl_v1",):
        if text == family or text.startswith(family + "_"):
            return family
    return text


class StrategyOwnershipRegistry:
    def __init__(self, path: str | os.PathLike[str] | None = None,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path or os.environ.get("STRATEGY_OWNERSHIP_PATH") or DEFAULT_OWNERSHIP_PATH)
        self._clock = clock
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ storage
    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            claims = payload["claims"]
            if not isinstance(claims, dict):
                raise ValueError("claims is not an object")
            for key, claim in claims.items():
                if not isinstance(claim, dict) or not claim.get("owner"):
                    raise ValueError(f"malformed claim {key!r}")
            return claims
        except Exception as exc:
            raise OwnershipUnreadable(f"{self.path}: {type(exc).__name__}: {exc}") from exc

    def _write(self, claims: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        data = json.dumps({"claims": claims}, sort_keys=True, indent=1)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        try:                                              # make the rename itself durable
            fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _locked(self):
        registry = self

        class _Guard:
            def __enter__(self_inner):
                registry._lock.acquire()
                registry.path.parent.mkdir(parents=True, exist_ok=True)
                self_inner.fh = open(registry.path.with_name(registry.path.name + ".lock"), "a+")
                fcntl.flock(self_inner.fh, fcntl.LOCK_EX)
                return self_inner

            def __exit__(self_inner, *exc):
                try:
                    fcntl.flock(self_inner.fh, fcntl.LOCK_UN)
                finally:
                    self_inner.fh.close()
                    registry._lock.release()
        return _Guard()

    # ------------------------------------------------------------------ api
    def claim(self, symbol: str, hold_side: str, owner: str, ref: str = "") -> bool:
        """Idempotent for the same owner. Returns True when a NEW claim was written."""
        owner = owner_family(owner)
        key = _key(symbol, hold_side)
        with self._locked():
            claims = self._read()
            current = claims.get(key)
            if current is not None:
                if current["owner"] == owner:
                    return False
                raise OwnershipConflict(
                    f"{key} is owned by {current['owner']!r} (ref={current.get('ref')}); "
                    f"{owner!r} may not claim it")
            claims[key] = {"owner": owner, "ref": str(ref), "claimed_at_ms": int(self._clock() * 1000)}
            self._write(claims)
            return True

    def release(self, symbol: str, hold_side: str, owner: str) -> bool:
        """Only the owner can release. Returns True when a claim was removed."""
        owner = owner_family(owner)
        key = _key(symbol, hold_side)
        with self._locked():
            claims = self._read()
            current = claims.get(key)
            if current is None:
                return False
            if current["owner"] != owner:
                raise OwnershipConflict(f"{key} is owned by {current['owner']!r}, not {owner!r}")
            del claims[key]
            self._write(claims)
            return True

    def owner_of(self, symbol: str, hold_side: str | None = None) -> str | None:
        """The claiming owner, or None. `hold_side=None` = any side of the symbol."""
        with self._lock:
            claims = self._read()
        if hold_side is not None:
            claim = claims.get(_key(symbol, hold_side))
            return claim["owner"] if claim else None
        prefix = f"{str(symbol).upper()}:"
        for key in sorted(claims):
            if key.startswith(prefix):
                return claims[key]["owner"]
        return None

    def claims(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._read())

    def owned_symbols(self, *, fail_closed_symbols: Iterable[str] = ()) -> dict[str, str]:
        """symbol -> owner for every claimed symbol.

        If the registry is unreadable, every symbol in `fail_closed_symbols` is reported as owned by
        'UNREADABLE_REGISTRY': legacy code must then leave it alone rather than adopt a position that
        may belong to a strategy running its own lifecycle."""
        try:
            claims = self.claims()
        except OwnershipUnreadable:
            return {str(s).upper(): "UNREADABLE_REGISTRY" for s in fail_closed_symbols}
        return {key.split(":", 1)[0]: claim["owner"] for key, claim in sorted(claims.items())}
