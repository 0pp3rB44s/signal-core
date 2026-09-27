"""The four operator questions, answered in one place.

    1. Is the bot alive?
    2. Is it safe?
    3. Can it trade right now?
    4. If not, why not?

The value here is the *ordering* of blockers. Several conditions can block
trading at once, and an operator who is shown four amber cards has to work out
which one actually matters. `primary_blocker` applies a fixed precedence —
liveness before safety, safety before configuration, configuration before data —
so the page answers "why not" with one sentence instead of a list to triage.

This module decides nothing and writes nothing. It reads state that other
components produced and reports what it means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dashboard_v3.core.status import Status


class Eligibility:
    READY = "READY"
    RISK_BLOCKED = "RISK BLOCKED"
    CONFIG_BLOCKED = "CONFIG BLOCKED"
    DATA_BLOCKED = "DATA BLOCKED"
    ENGINE_DOWN = "ENGINE DOWN"
    UNKNOWN = "UNKNOWN"


#: (key, severity) — lower severity wins. An engine that is not running makes
#: every other blocker academic, and an unknown state must never outrank a
#: proven one: "we cannot tell" is not more urgent than "it is frozen".
PRECEDENCE = (
    ("engine_down", 10),
    ("runtime_sha_mismatch", 20),
    ("weekly_freeze", 30),
    ("daily_stop", 35),
    ("unresolved_intents", 40),
    ("live_entry_disabled", 50),
    ("no_enabled_strategy", 55),
    ("stale_heartbeat", 60),
    ("stale_market_data", 70),
    ("unknown_state", 90),
)

BLOCKER_CLASS = {
    "engine_down": Eligibility.ENGINE_DOWN,
    "runtime_sha_mismatch": Eligibility.CONFIG_BLOCKED,
    "weekly_freeze": Eligibility.RISK_BLOCKED,
    "daily_stop": Eligibility.RISK_BLOCKED,
    "unresolved_intents": Eligibility.RISK_BLOCKED,
    "live_entry_disabled": Eligibility.CONFIG_BLOCKED,
    "no_enabled_strategy": Eligibility.CONFIG_BLOCKED,
    "stale_heartbeat": Eligibility.DATA_BLOCKED,
    "stale_market_data": Eligibility.DATA_BLOCKED,
    "unknown_state": Eligibility.UNKNOWN,
}

BLOCKER_TEXT = {
    "engine_down": "Engine is not running",
    "runtime_sha_mismatch": "Runtime SHA does not match the deployed SHA",
    "weekly_freeze": "Weekly loss freeze is active",
    "daily_stop": "Daily loss stop is active",
    "unresolved_intents": "Unresolved order intents block startup recovery",
    "live_entry_disabled": "Live entry is disabled for the strategy",
    "no_enabled_strategy": "No entry-enabled strategy is configured",
    "stale_heartbeat": "Engine heartbeat is stale",
    "stale_market_data": "Strategy signal data is stale",
    "unknown_state": "Trade eligibility cannot be determined",
}


@dataclass(frozen=True)
class Blocker:
    key: str
    severity: int
    label: str
    kind: str
    detail: str | None = None


@dataclass(frozen=True)
class Verdict:
    eligibility: str
    status: Status
    primary: Blocker | None
    secondary: tuple[Blocker, ...] = field(default_factory=tuple)

    @property
    def why(self) -> str:
        if self.primary is None:
            return "No blockers detected" if self.eligibility == Eligibility.READY else "UNKNOWN"
        return self.primary.detail or self.primary.label


def _blocker(key: str, detail: str | None = None) -> Blocker:
    severity = dict(PRECEDENCE).get(key, 99)
    return Blocker(key=key, severity=severity, label=BLOCKER_TEXT.get(key, key),
                   kind=BLOCKER_CLASS.get(key, Eligibility.UNKNOWN), detail=detail)


def assess(
    *,
    engine_running: bool | None,
    heartbeat_stale: bool | None,
    runtime_sha: str | None,
    deployed_sha: str | None,
    weekly_frozen: bool | None,
    daily_stop_active: bool | None = None,
    unresolved_intents: int | None = None,
    live_entry_enabled: bool | None,
    enabled_strategies: list[str] | None = None,
    signal_data_stale: bool | None = None,
) -> Verdict:
    """Collect every blocker, then report the one that actually decides."""
    found: list[Blocker] = []

    if engine_running is False:
        found.append(_blocker("engine_down"))
    elif engine_running is None:
        found.append(_blocker("unknown_state", "Engine state unknown"))

    if runtime_sha and deployed_sha and not (
        runtime_sha.startswith(deployed_sha) or deployed_sha.startswith(runtime_sha)
    ):
        found.append(_blocker(
            "runtime_sha_mismatch",
            f"Runtime {runtime_sha[:7]} != deployed {deployed_sha[:7]}",
        ))

    if weekly_frozen is True:
        found.append(_blocker("weekly_freeze"))
    if daily_stop_active is True:
        found.append(_blocker("daily_stop"))
    if unresolved_intents:
        found.append(_blocker("unresolved_intents", f"{unresolved_intents} unresolved"))

    if live_entry_enabled is False:
        found.append(_blocker("live_entry_disabled"))
    if enabled_strategies is not None and not enabled_strategies:
        found.append(_blocker("no_enabled_strategy"))

    if heartbeat_stale is True:
        found.append(_blocker("stale_heartbeat"))
    if signal_data_stale is True:
        found.append(_blocker("stale_market_data"))

    if not found:
        if engine_running and weekly_frozen is False and live_entry_enabled:
            return Verdict(Eligibility.READY, Status.HEALTHY, None)
        # Nothing is blocking, but not everything was observable either.
        return Verdict(Eligibility.UNKNOWN, Status.UNKNOWN, _blocker("unknown_state"))

    found.sort(key=lambda b: b.severity)
    primary = found[0]
    status = {
        Eligibility.ENGINE_DOWN: Status.OFFLINE,
        Eligibility.RISK_BLOCKED: Status.BLOCKED,
        Eligibility.CONFIG_BLOCKED: Status.BLOCKED,
        Eligibility.DATA_BLOCKED: Status.STALE,
        Eligibility.UNKNOWN: Status.UNKNOWN,
    }.get(primary.kind, Status.DEGRADED)
    return Verdict(primary.kind, status, primary, tuple(found[1:]))
