"""The single definition of operational state for the whole dashboard.

Every page derives its badges from here. The v2 dashboard computed "healthy"
independently in several panels, so the same fact could read green on one card
and red on another. There is exactly one ladder now.

Rule that outranks all others: absence of evidence is never health. A missing
file, an unparseable payload or an unreachable API resolves to UNKNOWN, never to
HEALTHY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Ordered worst-last so aggregation is a max()."""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def is_nominal(self) -> bool:
        return self is Status.HEALTHY


#: Severity ordering. UNKNOWN sits above BLOCKED deliberately: "we cannot tell"
#: demands attention at least as loudly as a known, understood block.
_RANK = {
    Status.HEALTHY: 0,
    Status.STALE: 1,
    Status.DEGRADED: 2,
    Status.BLOCKED: 3,
    Status.OFFLINE: 4,
    Status.UNKNOWN: 5,
}

#: Tone drives colour AND iconography, so state never depends on colour alone.
TONE = {
    Status.HEALTHY: "ok",
    Status.STALE: "warn",
    Status.DEGRADED: "warn",
    Status.BLOCKED: "block",
    Status.OFFLINE: "crit",
    Status.UNKNOWN: "unknown",
}

#: Non-colour redundant glyph for every state (WCAG: never colour alone).
GLYPH = {
    Status.HEALTHY: "●",
    Status.STALE: "◐",
    Status.DEGRADED: "▲",
    Status.BLOCKED: "■",
    Status.OFFLINE: "✕",
    Status.UNKNOWN: "?",
}


@dataclass(frozen=True)
class Signal:
    """One evaluated fact: a state plus the human reason behind it."""

    key: str
    label: str
    status: Status
    detail: str = ""
    hint: str = ""

    @property
    def tone(self) -> str:
        return TONE[self.status]

    @property
    def glyph(self) -> str:
        return GLYPH[self.status]


@dataclass
class SignalSet:
    """A group of signals that rolls up to its worst member."""

    signals: list[Signal] = field(default_factory=list)

    def add(self, signal: Signal) -> None:
        self.signals.append(signal)

    @property
    def status(self) -> Status:
        if not self.signals:
            return Status.UNKNOWN
        return max((s.status for s in self.signals), key=lambda s: s.rank)

    @property
    def worst(self) -> list[Signal]:
        """Signals at the roll-up severity, i.e. what actually needs attention."""
        top = self.status
        return [s for s in self.signals if s.status is top and not top.is_nominal]

    def by_key(self, key: str) -> Signal | None:
        return next((s for s in self.signals if s.key == key), None)


def worst(*statuses: Status) -> Status:
    """Aggregate several states into the most severe one."""
    present = [s for s in statuses if s is not None]
    if not present:
        return Status.UNKNOWN
    return max(present, key=lambda s: s.rank)


def freshness(
    age_seconds: float | None,
    *,
    stale_after: float,
    offline_after: float | None = None,
) -> Status:
    """Classify an age. ``None`` means we could not measure it -> UNKNOWN."""
    if age_seconds is None:
        return Status.UNKNOWN
    if offline_after is not None and age_seconds >= offline_after:
        return Status.OFFLINE
    if age_seconds >= stale_after:
        return Status.STALE
    return Status.HEALTHY


__all__ = ["GLYPH", "TONE", "Signal", "SignalSet", "Status", "freshness", "worst"]
