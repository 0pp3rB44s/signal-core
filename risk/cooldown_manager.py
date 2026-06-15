from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(slots=True)
class CooldownStatus:
    symbol: str
    active: bool
    reason: str = ""
    remaining_minutes: int = 0
    until: str = ""


class SymbolCooldownManager:
    """Central symbol cooldown helper.

    This manager is deliberately small and side-effect free except for the supplied state store.
    It keeps revenge/re-entry protection in one place so runner/execution logic does not drift.
    """

    def __init__(self, state_store) -> None:
        self.state_store = state_store

    @staticmethod
    def normalize_reason(reason: str | None) -> str:
        cleaned = str(reason or "cooldown").strip().lower().replace(" ", "_")
        return cleaned or "cooldown"

    def get(self, symbol: str) -> CooldownStatus:
        symbol = symbol.upper()
        self.prune_expired()
        data = self.state_store.load(default={}) or {}
        cooldown = data.get(symbol) or {}
        until_raw = str(cooldown.get("until") or "")
        reason = self.normalize_reason(cooldown.get("reason"))

        if not until_raw:
            return CooldownStatus(symbol=symbol, active=False)

        try:
            until_dt = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            remaining_seconds = (until_dt - now).total_seconds()
        except Exception:
            return CooldownStatus(symbol=symbol, active=False)

        if remaining_seconds <= 0:
            return CooldownStatus(symbol=symbol, active=False, reason=reason, until=until_raw)

        remaining_minutes = max(1, int(remaining_seconds // 60))
        return CooldownStatus(
            symbol=symbol,
            active=True,
            reason=reason,
            remaining_minutes=remaining_minutes,
            until=until_raw,
        )

    def set(self, symbol: str, *, minutes: int, reason: str = "cooldown") -> CooldownStatus:
        symbol = symbol.upper()
        reason = self.normalize_reason(reason)
        minutes = max(1, int(minutes or 1))
        self.prune_expired()
        now = datetime.now(timezone.utc)
        until = now + timedelta(minutes=minutes)

        data = self.state_store.load(default={}) or {}
        data[symbol] = {
            "symbol": symbol,
            "reason": reason,
            "created_at": now.isoformat(),
            "until": until.isoformat(),
            "duration_minutes": minutes,
        }
        self.state_store.save(data)
        return self.get(symbol)

    def set_cooldown(self, symbol: str, *, minutes: int, reason: str = "cooldown") -> CooldownStatus:
        return self.set(symbol, minutes=minutes, reason=reason)

    def clear(self, symbol: str) -> None:
        symbol = symbol.upper()
        data = self.state_store.load(default={}) or {}
        if symbol in data:
            data.pop(symbol, None)
            self.state_store.save(data)

    def clear_cooldown(self, symbol: str) -> None:
        self.clear(symbol)

    def prune_expired(self) -> int:
        data = self.state_store.load(default={}) or {}
        removed = 0
        now = datetime.now(timezone.utc)
        cleaned = {}

        for symbol, payload in data.items():
            until_raw = str((payload or {}).get("until") or "")
            if not until_raw:
                removed += 1
                continue
            try:
                until_dt = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
            except Exception:
                removed += 1
                continue
            if until_dt <= now:
                removed += 1
                continue
            normalized_symbol = str(symbol).upper()
            cleaned[normalized_symbol] = {
                **payload,
                "symbol": normalized_symbol,
                "reason": self.normalize_reason((payload or {}).get("reason")),
            }

        if removed:
            self.state_store.save(cleaned)
        return removed

    def is_active(self, symbol: str) -> bool:
        return self.get(symbol).active

    def as_log_payload(self, symbol: str) -> dict[str, Any] | None:
        status = self.get(symbol)
        if not status.active:
            return None
        return {
            "symbol": status.symbol,
            "reason": status.reason,
            "remaining_minutes": status.remaining_minutes,
            "until": status.until,
        }