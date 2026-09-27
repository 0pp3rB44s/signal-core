"""LSRL daily realized-R stop (pure). -2R realized blocks NEW setups until the next UTC day."""
from __future__ import annotations

from dataclasses import dataclass

from lsrl.core.config import LSRLConfig

DAY_MS = 86_400_000


@dataclass(slots=True)
class DailyRiskState:
    day: int = -1
    realized_r: float = 0.0
    blocked_day: int = -2

    def _roll(self, t_ms: int) -> None:
        day = t_ms // DAY_MS
        if day != self.day:
            self.day = day
            self.realized_r = 0.0

    def record_close(self, t_ms: int, r_multiple: float, cfg: LSRLConfig) -> None:
        self._roll(t_ms)
        self.realized_r += r_multiple
        if self.realized_r <= cfg.daily_stop_r:
            self.blocked_day = self.day

    def blocked(self, t_ms: int) -> bool:
        self._roll(t_ms)
        return self.blocked_day == self.day

    def to_dict(self) -> dict:
        return {"day": self.day, "realized_r": self.realized_r, "blocked_day": self.blocked_day}

    @classmethod
    def from_dict(cls, data: dict) -> "DailyRiskState":
        return cls(day=data["day"], realized_r=data["realized_r"], blocked_day=data["blocked_day"])
