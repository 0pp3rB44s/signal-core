"""Schedulable production owner for the frozen pilot; live arm defaults false."""
from __future__ import annotations
import time
from pathlib import Path
from funding_pilot.bitget_exchange import BitgetPilotExchangePort
from funding_pilot.canonical import CanonicalFundingPilot
from funding_pilot.core import PilotConfig, PilotLedger, PilotRuntime, PilotSignal
from funding_pilot.signals import FrozenFundingCrowdingSignalPoller


class CanonicalFundingPilotRunner:
    def __init__(self, *, execution_service, position_manager, spec_path: Path,
                 ledger_path: Path, armed_live: bool = False, signal_poller=None) -> None:
        self.armed_live = bool(armed_live)
        self.ledger = PilotLedger(ledger_path)
        self.exchange = BitgetPilotExchangePort(execution_service.client, self.ledger, armed_live=self.armed_live)
        config = PilotConfig(spec_path=spec_path, state_path=ledger_path, orders_enabled=self.armed_live)
        self.runtime = PilotRuntime(config, self.ledger, self.exchange)
        self.canonical = CanonicalFundingPilot(self.runtime, execution_service, position_manager)
        self.signal_poller = signal_poller or FrozenFundingCrowdingSignalPoller(
            client=execution_service.client, ledger=self.ledger, spec_path=spec_path,
        )

    def startup(self):
        recovered = self.canonical.recover()
        self.ledger.append("HEARTBEAT", {"phase": "startup", "armed_live": self.armed_live,
                                         "pilot_nav": recovered["pilot_nav"]})
        return recovered

    def tick(self, *, now_ms: int | None = None):
        now_ms = int(now_ms or time.time() * 1000)
        state = self.canonical.recover()
        exits = self.canonical.process_time_exits(now_ms=now_ms) if self.armed_live else []
        signals = list(self.signal_poller() or []) if callable(self.signal_poller) else []
        decisions = []
        for signal in signals:
            if not isinstance(signal, PilotSignal):
                raise TypeError("signal poller must return PilotSignal")
            if self.armed_live:
                decisions.append(self.canonical.process_signal(signal))
            else:
                plan = self.canonical.build_plan(signal)
                decisions.append({"status": "NO_ORDER", "plan_id": plan.plan_id,
                                  "notional": plan.position_notional_usdt})
        self.ledger.append("HEARTBEAT", {"phase": "tick", "armed_live": self.armed_live,
                                         "pilot_nav": state["pilot_nav"], "signals": len(signals),
                                         "exits": len(exits)})
        return {"state": state, "decisions": decisions, "exits": exits}
