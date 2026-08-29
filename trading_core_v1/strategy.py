"""Abstract strategy interface.

A Strategy answers ONE question: "given everything known up to and including
this bar, what is my directional opinion?" That's it.

Hard rules (enforced by review + tests, not by a sandbox):
  - NO order placement.
  - NO exchange calls (no `clients.*` imports).
  - NO risk sizing (that is trading_core_v1/risk.py's job).
  - NO Telegram / notification calls.

The SAME Strategy implementation must be usable for both backtesting
(trading_core_v1/backtest.py) and future live execution (trading_core_v1/
executor.py, currently hard-disabled) -- there is no separate backtest-only or
live-only strategy code path.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from trading_core_v1.data import Candle
from trading_core_v1.models import Signal


class Strategy(ABC):
    name: str = "unnamed_strategy"

    @abstractmethod
    def evaluate(self, history: Sequence[Candle], current_bar: Candle) -> Signal:
        """Return a Signal for `current_bar`.

        `history` contains only bars strictly before `current_bar` (see
        trading_core_v1.data.sequential_feed) -- using anything else as
        "future" information is a lookahead bug in the caller, not something
        this interface can prevent, so callers must always drive strategies
        through sequential_feed().

        Must be a pure function of (history, current_bar) plus whatever
        strategy-internal state the strategy explicitly documents it needs
        (e.g. a moving indicator). Must not raise for FLAT conditions --
        return Signal(side=Side.FLAT, ...) instead.
        """
        raise NotImplementedError
