"""Risk-gate decisions as recorded INPUTS, so a session replays without the live gate."""
from __future__ import annotations

from lsrl.core.gates import RiskDecision, RiskGate, RiskProposal


class RecordingRiskGate:
    """Wraps a real gate and records every (proposal -> decision) via `sink`."""

    def __init__(self, inner: RiskGate, sink) -> None:
        self.inner, self.sink = inner, sink

    def check(self, proposal: RiskProposal) -> RiskDecision:
        try:
            decision = self.inner.check(proposal)
        except Exception as exc:                       # recorded as a fail-closed decision
            decision = RiskDecision(False, (f"RISK_GATE_ERROR:{type(exc).__name__}",), None, "ERROR")
        self.sink(proposal.t_ms, proposal.setup_id, {
            "allowed": decision.allowed, "reasons": list(decision.reasons),
            "applied_risk_fraction": decision.applied_risk_fraction, "source": decision.source})
        return decision


class ReplayRiskGate:
    """Returns the recorded decision for each setup; unknown setup => reject (fail closed)."""

    def __init__(self, decisions: dict[str, dict]) -> None:
        self.decisions = decisions

    def check(self, proposal: RiskProposal) -> RiskDecision:
        d = self.decisions.get(proposal.setup_id)
        if d is None:
            return RiskDecision(False, ("REPLAY_NO_RECORDED_GATE_DECISION",), None, "REPLAY")
        return RiskDecision(d["allowed"], tuple(d["reasons"]), d["applied_risk_fraction"], d["source"])
