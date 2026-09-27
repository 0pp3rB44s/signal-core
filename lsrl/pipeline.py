"""FeedPipeline: raw feed events -> BookObserver -> LSRLEngine. The ONE processing path.

Every run mode (live runtime, simulated modes, replay of a recording, backtest) feeds events through
this class, so the decision logic between "bytes off the wire" and "engine decisions" cannot differ by
mode. Raw feed events (`RawBook`, `RawTrade`, `FeedMarker`) never reach the engine directly: the observer
turns them into causally reconstructed `DepthSnapshot`s, `TradeTick`s and `FeedHealth`. Everything else
(bars, NAV, adapter fills/orders, already-derived market events) goes straight to the engine.
"""
from __future__ import annotations

import json
from typing import Any

from lsrl.core.types import RAW_FEED_TYPES, FeedMarker, Intent, Record, TimerTick

RESTORE = "RESTORE"        # live + replay: a session boundary restores the engine state recorded at that boundary
CONTINUE = "CONTINUE"      # backtest: keep the backtest's own account; only the market view is reset


class FeedPipeline:
    def __init__(self, engine: Any, observer: Any, *, session_mode: str = RESTORE) -> None:
        self.engine, self.observer, self.session_mode = engine, observer, session_mode

    def process(self, ev: Any) -> tuple[list[Intent], list[Record]]:
        intents: list[Intent] = []
        records: list[Record] = []
        if isinstance(ev, FeedMarker) and ev.kind == "SESSION_START" and self.session_mode == RESTORE:
            state = None
            if ev.detail:
                try:
                    state = json.loads(ev.detail).get("engine_state")
                except (ValueError, AttributeError):
                    state = None
            self.engine.begin_session(state)
        derived = self.observer.on_event(ev) if isinstance(ev, (*RAW_FEED_TYPES, TimerTick)) else []
        for d in derived:
            i, r = self.engine.on_event(d)
            intents.extend(i)
            records.extend(r)
        if not isinstance(ev, RAW_FEED_TYPES):
            i, r = self.engine.on_event(ev)
            intents.extend(i)
            records.extend(r)
        return intents, records
