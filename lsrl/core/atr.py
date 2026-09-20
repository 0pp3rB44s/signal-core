"""Wilder ATR over COMPLETED bars only (pure, streaming)."""
from __future__ import annotations


class WilderATR:
    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._prev_close: float | None = None
        self._seed: list[float] = []
        self._value: float | None = None
        self.bars_seen = 0

    def update(self, high: float, low: float, close: float) -> None:
        self.bars_seen += 1
        if self._prev_close is None:
            self._prev_close = close
            return
        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self._value is None:
            self._seed.append(tr)
            if len(self._seed) == self.period:
                self._value = sum(self._seed) / self.period
                self._seed = []
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period

    @property
    def value(self) -> float | None:
        """None until period+1 completed bars have been seen (fail closed)."""
        return self._value

    def state(self) -> dict:
        return {"prev_close": self._prev_close, "seed": list(self._seed),
                "value": self._value, "bars_seen": self.bars_seen}

    @classmethod
    def from_state(cls, period: int, state: dict) -> "WilderATR":
        atr = cls(period)
        atr._prev_close = state["prev_close"]
        atr._seed = list(state["seed"])
        atr._value = state["value"]
        atr.bars_seen = state["bars_seen"]
        return atr
