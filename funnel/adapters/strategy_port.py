"""SIGNAL_CORE_FUNNEL_V1 seen through the SAME strategy port LSRL_V1 uses.

No new gate is invented here. lsrl.adapters.strategy_port.check_intents() already validates any
PlaceEntry/CancelOrder/SetStop/SetTargets against the intent vocabulary regardless of which engine
produced them, and lsrl.adapters.strategy_port.StrategyPort is a structural Protocol (strategy_id,
spec_digest, symbol, halt) - this class exists only to supply those three identity fields for the
funnel engine, exactly as LsrlStrategyPort does for LSRLEngine.
"""
from __future__ import annotations

from typing import Any

from funnel.core.config import STRATEGY_ID


class FunnelStrategyPort:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.strategy_id, self.spec_digest, self.symbol = STRATEGY_ID, engine.cfg.digest(), engine.cfg.symbol

    def halt(self, reason: str) -> None:
        self.engine.halt(reason)
