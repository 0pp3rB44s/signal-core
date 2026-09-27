"""The strategy port: the ONE contract a strategy must satisfy to reach the exchange, and the only vocabulary it may speak.

A strategy is a PURE decision core. It consumes market/execution events and emits INTENTS - `PlaceEntry`, `CancelOrder`, `SetStop`, `SetTargets` - and nothing
else. It never holds a client, a key, a socket or a file handle. Everything that can touch the exchange is on the other side of `LSRLLiveExecutor.apply()`,
and that path, for EVERY strategy, in this order:

    strategy core -> its RiskGate (the central RiskManager verdict, recorded per setup)         [a strategy that skips it has no verdict]
                  -> LiveDriver -> executor.apply():
                        intent vocabulary check (this module)          unknown/malformed intent -> refused + halt
                        LiveAuthority: PROTECT_ONLY / capacity monitor / capital band   (ceremony consumed in this process)
                        ownership claim, foreign position/order refusal, exposure state machine, exchange reconciliation
                        mandatory central authorization for THIS order (the recorded verdict), clock gate, leverage preflight
                        loss limits: an unrecorded close blocks new entries (`CENTRAL_CLOSE_RECORD_PENDING`), RiskManager kill switches
                        process-down protection (order-level preset stop) and the verified-flat emergency path

There is no second path: `tests/test_lsrl_strategy_port_no_bypass.py` scans the source tree for any other route to an order and runs a toy strategy that
tries to skip each gate. Adding a strategy means implementing `StrategyPort`, a reviewed change to the ceremony's identity, and Agent C's approval artifact;
nothing here changes what LSRL_V1 decides.
"""
from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

from lsrl.core.types import CancelOrder, Direction, PlaceEntry, SetStop, SetTargets

INTENT_TYPES = (PlaceEntry, CancelOrder, SetStop, SetTargets)


class IntentRefused(RuntimeError):
    """The strategy emitted something outside the intent vocabulary, or an intent that is not well formed."""


@runtime_checkable
class StrategyPort(Protocol):
    strategy_id: str
    spec_digest: str
    symbol: str

    def halt(self, reason: str) -> None: ...


class LsrlStrategyPort:
    """LSRL_V1 seen through the port: identity only. The engine itself is untouched."""

    def __init__(self, engine: Any) -> None:
        from lsrl.core.config import STRATEGY_ID
        self.engine = engine
        self.strategy_id, self.spec_digest, self.symbol = STRATEGY_ID, engine.cfg.digest(), engine.cfg.symbol

    def halt(self, reason: str) -> None:
        self.engine.halt(reason)


def _positive(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def check_intents(intents: list[Any]) -> None:
    """Every intent must be exactly one of the four known types (a subclass, a dict or a look-alike is not), and an order must carry a positive
    finite price and quantity. Raises IntentRefused; the executor then places nothing from this batch and halts new entries."""
    for it in intents:
        if type(it) not in INTENT_TYPES:
            raise IntentRefused(f"UNKNOWN_INTENT_TYPE:{type(it).__name__}")
        if isinstance(it, PlaceEntry):
            if not (_positive(it.price) and _positive(it.qty)) or not isinstance(it.direction, Direction) or not it.setup_id or not it.tag:
                raise IntentRefused(f"MALFORMED_ENTRY:{getattr(it, 'tag', '?')}")
        elif isinstance(it, SetStop):
            if not _positive(it.price) or isinstance(it.qty, bool) or not isinstance(it.qty, (int, float)) or not math.isfinite(it.qty) or it.qty < 0:
                raise IntentRefused(f"MALFORMED_STOP:{it.setup_id}")
        elif isinstance(it, SetTargets):
            if any(not (_positive(t.price) and _positive(t.qty)) for t in it.targets):
                raise IntentRefused(f"MALFORMED_TARGETS:{it.setup_id}")
